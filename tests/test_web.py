"""The web interface: auth, CSRF, rate limits, resolutions, review, headers.

Tests always use the file-fallback token (the OS keyring is patched away)
and never bind a socket — FastAPI's TestClient drives the app in-process.
"""

import json
import re

import pytest
from typer.testing import CliRunner

import muster.web.auth as auth_module
from muster.assist import REVIEW_FILE_NAME, MappingProposal, ReviewFile, write_review_file
from muster.cli import app as cli_app
from muster.credentials import clear_registered_secrets
from muster.web import create_app
from muster.web.data import load_resolutions

runner = CliRunner()

CONFIG = """\
fields:
  - name: customer_id
    type: string
    required: true
  - name: spend
    type: float
    rules:
      - rule: range
        min: 0
        severity: error
sources: ["*.csv"]
validation:
  keys: ["customer_id"]
"""

CSV = "customer_id,spend,mystery\nC-1,10.5,a\nC-2,not-a-number,b\n"


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch):
    # Tests must never read or write the user's actual keyring.
    monkeypatch.setattr(auth_module, "_keyring_token", lambda: (None, ""))
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "muster.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "data.csv").write_text(CSV, encoding="utf-8")
    assert runner.invoke(cli_app, ["run"]).exit_code == 2  # deliberate error
    return tmp_path


@pytest.fixture()
def client(project):
    from fastapi.testclient import TestClient

    return TestClient(create_app(project, project / "muster.yaml"))


def _token(project):
    return (project / auth_module.TOKEN_FILE).read_text(encoding="utf-8").strip()


def _login(client, project):
    response = client.post(
        "/login", data={"token": _token(project)}, follow_redirects=False
    )
    assert response.status_code == 303
    return client


def _csrf(client) -> str:
    page = client.get("/").text
    match = re.search(r'name="csrf" value="([^"]+)"', page)
    assert match, "no CSRF token in the page"
    return match.group(1)


def test_pages_redirect_and_posts_401_without_a_session(client):
    for path in ("/", "/exceptions", "/review", "/report"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"
    assert client.post("/run", data={"csrf": "x"}).status_code == 401
    assert (
        client.post(
            "/exceptions/0123456789abcdef", data={"csrf": "x", "action": "resolved"}
        ).status_code
        == 401
    )


def test_health_probes_answer_without_a_session(client):
    live = client.get("/healthz")
    assert live.status_code == 200
    assert live.json() == {"status": "ok"}
    # No session, but the security headers still apply.
    assert live.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in live.headers

    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok"}


def test_readyz_degrades_on_a_broken_config_without_leaking_paths(client, project):
    (project / "muster.yaml").write_text("fields: [", encoding="utf-8")
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body == {"status": "unavailable", "reason": "configuration does not parse"}
    assert str(project) not in json.dumps(body)


def test_readyz_degrades_when_the_runs_directory_is_not_writable(client, project):
    runs = project / "runs"
    runs.chmod(0o500)
    try:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["reason"] == "runs directory is not writable"
    finally:
        runs.chmod(0o700)


def test_wrong_token_is_401_and_right_token_sets_a_strict_cookie(client, project):
    wrong = client.post("/login", data={"token": "not-the-token"})
    assert wrong.status_code == 401

    good = client.post(
        "/login", data={"token": _token(project)}, follow_redirects=False
    )
    assert good.status_code == 303
    cookie = good.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie

    assert client.get("/").status_code == 200


def test_login_attempts_are_rate_limited(client):
    responses = [
        client.post("/login", data={"token": "wrong"}).status_code for _ in range(6)
    ]
    assert responses[:5] == [401] * 5
    assert responses[5] == 429


def test_token_file_fallback_is_owner_only(project):
    create_app(project, project / "muster.yaml")
    path = project / auth_module.TOKEN_FILE
    assert path.is_file()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_mutations_demand_the_session_csrf(client, project, monkeypatch):
    monkeypatch.setattr("muster.web.app.run_once", lambda root, config: 0)
    _login(client, project)
    assert client.post("/run", data={"csrf": "forged"}).status_code == 403
    response = client.post(
        "/run", data={"csrf": _csrf(client)}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "run-started" in response.headers["location"]


def test_exception_resolution_appends_to_the_audit_log(client, project):
    _login(client, project)
    page = client.get("/exceptions").text
    ids = re.findall(r'action="/exceptions/([0-9a-f]{16})"', page)
    assert ids, "expected unresolved exceptions with decision forms"
    csrf = _csrf(client)
    before = (project / "output" / "exceptions.csv").read_bytes()

    response = client.post(
        f"/exceptions/{ids[0]}",
        data={"csrf": csrf, "action": "resolved", "note": "fixed at the source"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    log_path = project / "runs" / "resolutions.jsonl"
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[-1]["action"] == "resolved"
    assert entries[-1]["note"] == "fixed at the source"
    assert entries[-1]["id"] == ids[0]

    # History is never mutated: the run's own record is untouched, and a
    # second decision appends rather than rewrites.
    assert (project / "output" / "exceptions.csv").read_bytes() == before
    client.post(
        f"/exceptions/{ids[0]}",
        data={"csrf": csrf, "action": "dismissed", "note": "second thoughts"},
    )
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(entries) == 2
    assert load_resolutions(project)[ids[0]]["action"] == "dismissed"

    page = client.get("/exceptions").text
    assert "dismissed" in page


def _correctable_id(client) -> str:
    page = client.get("/exceptions").text
    ids = re.findall(r'action="/exceptions/([0-9a-f]{16})/correct"', page)
    assert ids, "expected a correctable error with an inline correction form"
    return ids[0]


def test_correcting_a_held_row_from_the_browser(client, project):
    _login(client, project)
    exception_id = _correctable_id(client)
    csrf = _csrf(client)

    response = client.post(
        f"/exceptions/{exception_id}/correct",
        data={"csrf": csrf, "value": "12.5", "note": "till receipt says 12.5"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    entries = [
        json.loads(line)
        for line in (project / "runs" / "resolutions.jsonl").read_text().splitlines()
    ]
    assert entries[-1]["action"] == "corrected"
    assert entries[-1]["corrected_values"] == {"spend": "12.5"}

    # The remediated filter and the dashboard tile both show the pending rerun.
    filtered = client.get("/exceptions?state=remediated").text
    assert "corrected" in filtered and "12.5" in filtered
    assert "applies on the next run" in filtered
    dashboard = client.get("/").text
    assert "corrected, awaiting rerun" in dashboard

    # The next run applies it: the held row rejoins the governed dataset and
    # the run is clean, so the CLI exits 0 this time.
    rerun = runner.invoke(cli_app, ["run"])
    assert rerun.exit_code == 0, rerun.output
    assert "1 row(s) recovered via remediation" in " ".join(rerun.output.split())
    dashboard = client.get("/").text
    assert "corrected, awaiting rerun" in dashboard  # tile remains, at zero


def test_corrections_are_validated_server_side(client, project):
    _login(client, project)
    exception_id = _correctable_id(client)
    csrf = _csrf(client)

    uncoercible = client.post(
        f"/exceptions/{exception_id}/correct",
        data={"csrf": csrf, "value": "still-bad", "note": "n"},
    )
    assert uncoercible.status_code == 422
    assert "cannot be coerced to float" in uncoercible.text

    out_of_range = client.post(
        f"/exceptions/{exception_id}/correct",
        data={"csrf": csrf, "value": "-5", "note": "n"},
    )
    assert out_of_range.status_code == 422
    assert "out of range" in out_of_range.text

    no_note = client.post(
        f"/exceptions/{exception_id}/correct",
        data={"csrf": csrf, "value": "12.5", "note": "  "},
    )
    assert no_note.status_code == 422
    assert "note is required" in no_note.text

    unknown = client.post(
        "/exceptions/0123456789abcdef/correct",
        data={"csrf": csrf, "value": "12.5", "note": "n"},
    )
    assert unknown.status_code == 404

    # Nothing above reached the audit log.
    assert not (project / "runs" / "resolutions.jsonl").exists()

    # A warning with no row number cannot be corrected.
    from muster.config import load_config
    from muster.web.data import load_exceptions

    rows = load_exceptions(project, load_config(project / "muster.yaml"))
    warning = next(r for r in rows if r.severity == "warning")
    refused = client.post(
        f"/exceptions/{warning.id}/correct",
        data={"csrf": csrf, "value": "12.5", "note": "n"},
    )
    assert refused.status_code == 422
    assert "only row-level errors" in refused.text


def test_corrections_demand_auth_and_csrf(client, project):
    assert (
        client.post(
            "/exceptions/0123456789abcdef/correct",
            data={"csrf": "x", "value": "1", "note": "n"},
        ).status_code
        == 401
    )
    _login(client, project)
    exception_id = _correctable_id(client)
    forged = client.post(
        f"/exceptions/{exception_id}/correct",
        data={"csrf": "forged", "value": "1", "note": "n"},
    )
    assert forged.status_code == 403


def test_resolution_input_is_validated(client, project):
    _login(client, project)
    csrf = _csrf(client)
    assert (
        client.post(
            "/exceptions/not-a-real-id!", data={"csrf": csrf, "action": "resolved"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/exceptions/0123456789abcdef", data={"csrf": csrf, "action": "shrug"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/exceptions/0123456789abcdef", data={"csrf": csrf, "action": "resolved"}
        ).status_code
        == 404
    )


def test_review_flow_in_the_browser(client, project):
    review = ReviewFile(
        generated_at="2026-07-12T00:00:00+00:00",
        provider="anthropic",
        model="claude-sonnet-5",
        proposals=[
            MappingProposal(
                column="mystery",
                target="spend",
                confidence=88,
                rationale="looks numeric",
                samples=["#.#"],
            )
        ],
    )
    write_review_file(review, project / REVIEW_FILE_NAME)

    _login(client, project)
    page = client.get("/review").text
    assert "mystery" in page and "looks numeric" in page

    response = client.post(
        "/review/0",
        data={"csrf": _csrf(client), "action": "accept"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    from muster.assist import load_review_file

    assert load_review_file(project / REVIEW_FILE_NAME).proposals[0].status == "accepted"

    assert (
        client.post(
            "/review/99", data={"csrf": _csrf(client), "action": "accept"}
        ).status_code
        == 404
    )


def test_dashboard_report_and_security_headers(client, project):
    _login(client, project)
    dashboard = client.get("/")
    assert "rows published" in dashboard.text
    assert "Trends across runs" in dashboard.text
    csp = dashboard.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'nonce-" in csp
    assert "'unsafe-inline'" not in csp
    assert dashboard.headers["x-content-type-options"] == "nosniff"
    assert dashboard.headers["referrer-policy"] == "no-referrer"

    report = client.get("/report")
    assert report.status_code == 200
    assert "Muster run report" in report.text
    report_csp = report.headers["content-security-policy"]
    assert "style-src 'unsafe-inline'" in report_csp  # the archived report's own styles
    assert "default-src 'none'" in report_csp  # …and nothing else, scripts included
    assert "script-src" not in report_csp


def test_logout_ends_the_session(client, project):
    _login(client, project)
    csrf = _csrf(client)
    response = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303
