"""REST target against a scripted transport: batching, auth, retries, 429.

The network is patched away at :func:`muster.targets.http._open`; sleeps are
captured, never slept.
"""

import email.message
import io
import json
import urllib.error

import polars as pl
import pytest

import muster.targets.http as http_module
from muster.config import RestTarget
from muster.credentials import clear_registered_secrets
from muster.targets.base import TargetError
from muster.targets.rest import RestRuntime


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


class FakeResponse:
    def __init__(self, body=b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("https://api.test/ingest", code, "err", message, io.BytesIO(b""))


class ScriptedTransport:
    """Answers each request from a script of responses or exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.sleeps = []

    def open(self, request, timeout):
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def install(self, monkeypatch):
        monkeypatch.setattr(http_module, "_open", self.open)
        monkeypatch.setattr(http_module, "_sleep", self.sleeps.append)


FRAME = pl.DataFrame(
    {
        "customer_id": [f"C-{i}" for i in range(1, 6)],
        "spend": [float(i) for i in range(1, 6)],
    }
)


def _runtime(**overrides) -> RestRuntime:
    settings = {
        "type": "rest",
        "url": "https://api.test/ingest",
        "batch_size": 2,
        "max_retries": 2,
    } | overrides
    return RestRuntime("ingest", RestTarget(**settings), ["customer_id"])


def test_batching_and_bearer_auth(monkeypatch):
    monkeypatch.setenv("MUSTER_REST_TOKEN", "tok-abcdef123456")
    transport = ScriptedTransport([FakeResponse(), FakeResponse(), FakeResponse()])
    transport.install(monkeypatch)

    outcome = _runtime().publish(FRAME)

    assert outcome.rows_sent == 5 and not outcome.failures
    assert len(transport.requests) == 3
    sizes = []
    for request in transport.requests:
        assert request.full_url == "https://api.test/ingest"
        assert request.get_method() == "POST"
        assert request.headers["Authorization"] == "Bearer tok-abcdef123456"
        body = json.loads(request.data.decode("utf-8"))
        sizes.append(len(body["records"]))
        assert all("customer_id" in record for record in body["records"])
    assert sizes == [2, 2, 1]


def test_api_key_auth_uses_the_configured_header(monkeypatch):
    monkeypatch.setenv("MUSTER_REST_TOKEN", "key-abcdef123456")
    transport = ScriptedTransport([FakeResponse()] * 3)
    transport.install(monkeypatch)

    _runtime(auth="api_key", api_key_header="X-Ingest-Key").publish(FRAME)

    # urllib capitalises header names on storage.
    assert transport.requests[0].headers["X-ingest-key"] == "key-abcdef123456"
    assert "Authorization" not in transport.requests[0].headers


def test_transient_500s_are_retried_with_growing_backoff(monkeypatch):
    monkeypatch.setenv("MUSTER_REST_TOKEN", "tok-abcdef123456")
    transport = ScriptedTransport(
        [_http_error(500), _http_error(503), FakeResponse()] + [FakeResponse()] * 2
    )
    transport.install(monkeypatch)

    outcome = _runtime().publish(FRAME)

    assert outcome.rows_sent == 5
    assert len(transport.sleeps) == 2
    first, second = transport.sleeps
    assert 0.25 <= first <= 0.5  # base 0.5 with jitter
    assert 0.5 <= second <= 1.0  # doubled
    assert len(transport.requests) == 5  # 3 attempts for batch 1, then 2 batches


def test_429_honours_retry_after(monkeypatch):
    monkeypatch.setenv("MUSTER_REST_TOKEN", "tok-abcdef123456")
    transport = ScriptedTransport(
        [_http_error(429, {"Retry-After": "7"}), FakeResponse()] + [FakeResponse()] * 2
    )
    transport.install(monkeypatch)

    outcome = _runtime().publish(FRAME)

    assert outcome.rows_sent == 5
    assert transport.sleeps == [7.0]


def test_a_hard_rejection_fails_fast_without_retries(monkeypatch):
    monkeypatch.setenv("MUSTER_REST_TOKEN", "tok-abcdef123456")
    transport = ScriptedTransport([_http_error(401)])
    transport.install(monkeypatch)

    outcome = _runtime().publish(FRAME)

    assert transport.sleeps == []
    assert len(transport.requests) == 1
    assert outcome.rows_sent == 0
    # Every unsent record is accounted for, keyed for retry later.
    assert [failure.key for failure in outcome.failures] == [
        "C-1",
        "C-2",
        "C-3",
        "C-4",
        "C-5",
    ]
    assert all("HTTP 401" in failure.message for failure in outcome.failures)


def test_exhausted_retries_record_the_unsent_rows(monkeypatch):
    monkeypatch.setenv("MUSTER_REST_TOKEN", "tok-abcdef123456")
    transport = ScriptedTransport([FakeResponse(), _http_error(500), _http_error(500), _http_error(500)])
    transport.install(monkeypatch)

    outcome = _runtime().publish(FRAME)

    # Batch 1 landed; batch 2 exhausted its two retries; batch 3 not attempted.
    assert outcome.rows_sent == 2
    assert [failure.key for failure in outcome.failures] == ["C-3", "C-4", "C-5"]


def test_no_auth_sends_no_credential_headers(monkeypatch):
    monkeypatch.delenv("MUSTER_REST_TOKEN", raising=False)
    transport = ScriptedTransport([FakeResponse()] * 3)
    transport.install(monkeypatch)

    _runtime(auth="none").publish(FRAME)

    headers = transport.requests[0].headers
    assert "Authorization" not in headers
    assert not any(key.lower().startswith("x-") for key in headers)
