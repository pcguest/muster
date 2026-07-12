"""Salesforce target against a scripted mock server — never a live org.

The transport is patched at :func:`muster.targets.http._open`. These tests
pin the OAuth2 request shapes, the composite upsert payload, per-record
error reporting with Salesforce error codes, and that every credential
stays out of output and manifests.
"""

import email.message
import json
import urllib.parse

import polars as pl
import pytest
from typer.testing import CliRunner

import muster.targets.http as http_module
from muster.cli import app
from muster.config import SalesforceTarget
from muster.credentials import REDACTED, clear_registered_secrets, redact_text
from muster.targets.salesforce import SalesforceRuntime

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture(autouse=True)
def _credentials(monkeypatch):
    monkeypatch.setenv("MUSTER_SF_CLIENT_ID", "client-id-123456")
    monkeypatch.setenv("MUSTER_SF_CLIENT_SECRET", "client-secret-abcdef")
    monkeypatch.setenv("MUSTER_SF_USERNAME", "integration@example.invalid")
    monkeypatch.setenv("MUSTER_SF_PASSWORD", "pw-with-token-xyz789")


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


TOKEN_REPLY = {
    "access_token": "00Dxx0000000000!AQEAQtoken9876543210",
    "instance_url": "https://demo-org.my.salesforce.com",
    "token_type": "Bearer",
}


class MockOrg:
    """A scripted stand-in for a Salesforce org."""

    def __init__(self, upsert_replies):
        self.upsert_replies = list(upsert_replies)
        self.requests: list = []

    def open(self, request, timeout):
        self.requests.append(request)
        if "/services/oauth2/token" in request.full_url:
            return FakeResponse(TOKEN_REPLY)
        return FakeResponse(self.upsert_replies.pop(0))

    def install(self, monkeypatch):
        monkeypatch.setattr(http_module, "_open", self.open)
        monkeypatch.setattr(http_module, "_sleep", lambda seconds: None)


def _spec(**overrides) -> SalesforceTarget:
    settings = {
        "type": "salesforce",
        "object": "Receival__c",
        "external_id_field": "Ticket_Id__c",
        "field_map": {
            "receival_id": "Ticket_Id__c",
            "tonnes": "Net_Tonnes__c",
        },
    } | overrides
    return SalesforceTarget(**settings)


FRAME = pl.DataFrame({"receival_id": ["R-1", "R-2"], "tonnes": [10.5, 20.0]})


def test_client_credentials_flow_and_upsert_payload(monkeypatch):
    org = MockOrg([[{"id": "a01", "success": True}, {"id": "a02", "success": True}]])
    org.install(monkeypatch)

    outcome = SalesforceRuntime("crm", _spec(), ["receival_id"]).publish(FRAME)

    assert outcome.rows_sent == 2 and not outcome.failures
    token_request, upsert_request = org.requests

    assert token_request.full_url == "https://login.salesforce.com/services/oauth2/token"
    form = urllib.parse.parse_qs(token_request.data.decode("utf-8"))
    assert form["grant_type"] == ["client_credentials"]
    assert form["client_id"] == ["client-id-123456"]
    assert form["client_secret"] == ["client-secret-abcdef"]
    assert "username" not in form

    assert upsert_request.get_method() == "PATCH"
    assert upsert_request.full_url == (
        "https://demo-org.my.salesforce.com/services/data/v62.0"
        "/composite/sobjects/Receival__c/Ticket_Id__c"
    )
    body = json.loads(upsert_request.data.decode("utf-8"))
    assert body["allOrNone"] is False
    assert body["records"] == [
        {"attributes": {"type": "Receival__c"}, "Ticket_Id__c": "R-1", "Net_Tonnes__c": 10.5},
        {"attributes": {"type": "Receival__c"}, "Ticket_Id__c": "R-2", "Net_Tonnes__c": 20.0},
    ]

    # Both the configured credentials and the returned access token redact.
    assert redact_text("client-secret-abcdef") == REDACTED
    assert redact_text(TOKEN_REPLY["access_token"]) == REDACTED


def test_username_password_flow_sends_the_password_grant(monkeypatch):
    org = MockOrg([[{"id": "a01", "success": True}, {"id": "a02", "success": True}]])
    org.install(monkeypatch)

    spec = _spec(auth_flow="username_password")
    SalesforceRuntime("crm", spec, ["receival_id"]).publish(FRAME)

    form = urllib.parse.parse_qs(org.requests[0].data.decode("utf-8"))
    assert form["grant_type"] == ["password"]
    assert form["username"] == ["integration@example.invalid"]
    assert form["password"] == ["pw-with-token-xyz789"]


def test_per_record_failures_carry_salesforce_error_codes(monkeypatch):
    org = MockOrg(
        [
            [
                {"id": "a01", "success": True},
                {
                    "success": False,
                    "errors": [
                        {
                            "statusCode": "REQUIRED_FIELD_MISSING",
                            "message": "Required fields are missing: [Grower__c]",
                            "fields": ["Grower__c"],
                        }
                    ],
                },
            ]
        ]
    )
    org.install(monkeypatch)

    outcome = SalesforceRuntime("crm", _spec(), ["receival_id"]).publish(FRAME)

    assert outcome.rows_sent == 1
    (failure,) = outcome.failures
    assert failure.key == "R-2"
    assert failure.code == "REQUIRED_FIELD_MISSING"
    assert "Grower__c" in failure.message


def test_rows_without_an_external_id_are_recorded_not_sent(monkeypatch):
    org = MockOrg([[{"id": "a01", "success": True}]])
    org.install(monkeypatch)

    frame = pl.DataFrame({"receival_id": ["R-1", None], "tonnes": [10.5, 20.0]})
    outcome = SalesforceRuntime("crm", _spec(), ["receival_id"]).publish(frame)

    assert outcome.rows_sent == 1
    (failure,) = outcome.failures
    assert failure.code == "MISSING_EXTERNAL_ID"
    body = json.loads(org.requests[1].data.decode("utf-8"))
    assert len(body["records"]) == 1  # the id-less row never left the machine


def test_batches_respect_the_200_record_ceiling(monkeypatch):
    replies = [
        [{"id": f"a{i}", "success": True} for i in range(200)],
        [{"id": "b0", "success": True}] * 50,
    ]
    org = MockOrg(replies)
    org.install(monkeypatch)

    frame = pl.DataFrame(
        {"receival_id": [f"R-{i}" for i in range(250)], "tonnes": [1.0] * 250}
    )
    outcome = SalesforceRuntime("crm", _spec(), ["receival_id"]).publish(frame)

    assert outcome.rows_sent == 250
    upserts = org.requests[1:]
    assert [len(json.loads(r.data.decode("utf-8"))["records"]) for r in upserts] == [200, 50]


CONFIG = """\
fields:
  - name: receival_id
    type: string
    required: true
  - name: tonnes
    type: float
sources: ["*.csv"]
validation:
  keys: ["receival_id"]
targets:
  crm:
    type: salesforce
    object: Receival__c
    external_id_field: Ticket_Id__c
    field_map:
      receival_id: Ticket_Id__c
      tonnes: Net_Tonnes__c
"""


def test_cli_publish_writes_salesforce_failures_to_exceptions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "muster.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "data.csv").write_text(
        "receival_id,tonnes\nR-1,10.5\nR-2,20.0\n", encoding="utf-8"
    )
    assert runner.invoke(app, ["run"]).exit_code == 0

    org = MockOrg(
        [
            [
                {"id": "a01", "success": True},
                {
                    "success": False,
                    "errors": [
                        {"statusCode": "DUPLICATE_VALUE", "message": "duplicate value found"}
                    ],
                },
            ]
        ]
    )
    org.install(monkeypatch)

    result = runner.invoke(app, ["publish", "crm"])
    assert result.exit_code == 2, result.output  # partial: one record failed
    flat = " ".join(result.output.split())
    assert "Published 1 of 2 row(s)" in flat
    assert "1 record(s) failed" in flat

    exceptions = (tmp_path / "output" / "publish-exceptions.csv").read_text(encoding="utf-8")
    assert "publish_failed" in exceptions
    assert "DUPLICATE_VALUE" in exceptions
    assert "R-2" in exceptions

    # No credential reached the terminal, the manifest or the exceptions file.
    manifests = "".join(
        p.read_text(encoding="utf-8") for p in (tmp_path / "runs").rglob("manifest.json")
    )
    for secret in ("client-secret-abcdef", TOKEN_REPLY["access_token"]):
        assert secret not in result.output
        assert secret not in manifests
        assert secret not in exceptions
