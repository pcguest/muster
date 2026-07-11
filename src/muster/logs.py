"""Logging configuration.

Library code logs through the standard :mod:`logging` module in a logfmt
style; it never prints. The CLI decides the level via ``--verbose``.
"""

from __future__ import annotations

import logging

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
