"""Secret handling: resolution from env and keyring, redaction everywhere."""

import io
import logging
import sys
import types

import pytest

from muster.credentials import (
    REDACTED,
    SecretError,
    SecretRedactingFilter,
    clear_registered_secrets,
    redact_text,
    register_secret,
    resolve_secret,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_resolves_from_environment_and_registers(monkeypatch):
    monkeypatch.setenv("MUSTER_TEST_SECRET", "  s3cr3t-value  ")
    assert resolve_secret("MUSTER_TEST_SECRET", "a test") == "s3cr3t-value"
    assert redact_text("the dsn holds s3cr3t-value inside") == f"the dsn holds {REDACTED} inside"


def test_falls_back_to_the_os_keyring(monkeypatch):
    monkeypatch.delenv("MUSTER_TEST_SECRET", raising=False)
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, name: (
        "from-keyring" if (service, name) == ("muster", "MUSTER_TEST_SECRET") else None
    )
    monkeypatch.setitem(sys.modules, "keyring", fake)
    assert resolve_secret("MUSTER_TEST_SECRET", "a test") == "from-keyring"


def test_missing_secret_error_names_both_homes_and_no_value(monkeypatch):
    monkeypatch.delenv("MUSTER_TEST_SECRET", raising=False)
    monkeypatch.setitem(sys.modules, "keyring", None)  # import fails cleanly
    with pytest.raises(SecretError) as excinfo:
        resolve_secret("MUSTER_TEST_SECRET", "the unit test")
    message = str(excinfo.value)
    assert "MUSTER_TEST_SECRET" in message
    assert "keyring set muster" in message


def test_redaction_replaces_longest_secret_first():
    register_secret("hunter2")
    register_secret("postgresql://user:hunter2@db.example/warehouse")
    text = "failed: postgresql://user:hunter2@db.example/warehouse timed out"
    redacted = redact_text(text)
    assert "hunter2" not in redacted
    assert redacted == f"failed: {REDACTED} timed out"


def test_short_values_are_not_registered():
    register_secret("ab")  # too short: redacting it would shred ordinary text
    assert redact_text("absolutely") == "absolutely"


def test_logging_filter_strips_registered_secrets():
    register_secret("tok-9f8e7d6c")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactingFilter())
    probe = logging.getLogger("muster.test.redaction")
    probe.addHandler(handler)
    probe.propagate = False
    try:
        probe.warning("request failed with token %s", "tok-9f8e7d6c")
    finally:
        probe.removeHandler(handler)
    output = stream.getvalue()
    assert "tok-9f8e7d6c" not in output
    assert REDACTED in output
