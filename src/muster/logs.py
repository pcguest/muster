"""Logging configuration.

Library code logs through the standard :mod:`logging` module; it never
prints. The CLI decides the level via ``--verbose``. The default output is
logfmt; setting ``MUSTER_LOG_FORMAT=json`` switches to one JSON object per
line (``ts``, ``level``, ``logger``, ``msg``) so container platforms and
SIEMs ingest it without parsing rules. Every handler carries a redaction
filter, and the JSON formatter redacts again over the final message
(including any traceback), so a registered secret (see
:mod:`muster.credentials`) can never reach a log line in either format.
"""

from __future__ import annotations

import json
import logging
import os

from muster.credentials import SecretRedactingFilter, redact_text

_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

LOG_FORMAT_VARIABLE = "MUSTER_LOG_FORMAT"


class _JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return json.dumps(
            {
                "ts": self.formatTime(record, _DATE_FORMAT),
                "level": record.levelname,
                "logger": record.name,
                # The handler filter has already redacted the message body;
                # redact again here so exception tracebacks are covered too.
                "msg": redact_text(message),
            },
            ensure_ascii=False,
        )


def configure_logging(verbose: bool = False) -> None:
    """Configure structured logging on stderr.

    Verbose mode shows the full pipeline trace; otherwise only warnings and
    errors surface, keeping stdout clean for command output.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format=_FORMAT,
        datefmt=_DATE_FORMAT,
    )
    json_output = os.environ.get(LOG_FORMAT_VARIABLE, "").strip().lower() == "json"
    for handler in logging.getLogger().handlers:
        if json_output:
            handler.setFormatter(_JsonFormatter())
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
