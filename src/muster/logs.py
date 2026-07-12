"""Logging configuration.

Library code logs through the standard :mod:`logging` module in a logfmt
style; it never prints. The CLI decides the level via ``--verbose``. Every
handler carries a redaction filter so a registered secret (see
:mod:`muster.credentials`) can never reach a log line.
"""

from __future__ import annotations

import logging

from muster.credentials import SecretRedactingFilter

_FORMAT = "ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


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
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
