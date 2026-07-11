"""Exception records: the audit trail for anything Muster could not process.

Unmapped columns, failed coercions, skipped files and missing required fields
all become records in exceptions.csv. Data is never dropped without a row
here.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import polars as pl

logger = logging.getLogger(__name__)

EXCEPTION_SCHEMA = {
    "file": pl.String,
    "row": pl.Int64,
    "column": pl.String,
    "value": pl.String,
    "reason": pl.String,
}


@dataclass(frozen=True)
class ExceptionRecord:
    """One thing Muster could not process, and why.

    ``row`` is the source row number (header is row 1); it is empty for
    file- or column-level exceptions.
    """

    file: str
    reason: str
    row: int | None = None
    column: str | None = None
    value: str | None = None


def write_exceptions(records: Sequence[ExceptionRecord], path: Path) -> None:
    """Write records to CSV; an empty run still gets a header-only file."""
    frame = pl.DataFrame(
        [asdict(record) for record in records], schema=EXCEPTION_SCHEMA
    ).select(list(EXCEPTION_SCHEMA))
    frame.write_csv(path)
    logger.debug("wrote exceptions path=%s count=%d", path, len(records))
