"""Logging output: logfmt by default, JSON on request, secrets in neither.

These run in a subprocess, exactly as a container platform sees the
process: MUSTER_LOG_FORMAT in the environment, log lines on stderr.
"""

import json
import os
import subprocess
import sys

from muster.credentials import REDACTED
from muster.logs import LOG_FORMAT_VARIABLE

_SECRET = "hunter2-super-secret"

_EMIT = f"""
import logging
from muster.credentials import register_secret
from muster.logs import configure_logging

configure_logging(verbose=True)
register_secret({_SECRET!r})
logging.getLogger("muster.test").warning("hello %s", "world")
logging.getLogger("muster.publish").error(
    "connect failed for postgres://user:{_SECRET}@db/prod"
)
try:
    raise RuntimeError("token {_SECRET} rejected")
except RuntimeError:
    logging.getLogger("muster.publish").exception("publish failed")
"""


def _stderr(log_format: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != LOG_FORMAT_VARIABLE}
    if log_format is not None:
        env[LOG_FORMAT_VARIABLE] = log_format
    result = subprocess.run(
        [sys.executable, "-c", _EMIT],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stderr


def test_default_output_is_logfmt():
    lines = _stderr(None).strip().splitlines()
    assert "ts=" in lines[0]
    assert "level=WARNING" in lines[0]
    assert "logger=muster.test" in lines[0]
    assert "msg=hello world" in lines[0]


def test_json_format_emits_one_object_per_line():
    lines = _stderr("json").strip().splitlines()
    first = json.loads(lines[0])
    assert set(first) == {"ts", "level", "logger", "msg"}
    assert first["level"] == "WARNING"
    assert first["logger"] == "muster.test"
    assert first["msg"] == "hello world"
    # The exception record folds its traceback into msg: still one JSON
    # object per stderr line once the parser consumes it.
    records = [json.loads(line) for line in lines if line.startswith("{")]
    assert any(r["level"] == "ERROR" for r in records)


def test_a_registered_secret_never_appears_in_json_output():
    output = _stderr("json")
    assert _SECRET not in output
    assert REDACTED in output


def test_json_tracebacks_are_redacted_too():
    lines = _stderr("json").strip().splitlines()
    exception_record = json.loads(lines[-1])
    assert exception_record["msg"].startswith("publish failed")
    assert "RuntimeError" in exception_record["msg"]
    assert _SECRET not in exception_record["msg"]
    assert REDACTED in exception_record["msg"]
