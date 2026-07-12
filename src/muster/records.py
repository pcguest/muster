"""Exception records: the audit trail for anything Muster could not process.

Unmapped columns, failed coercions, rule violations, conflicts and skipped
files all become records in exceptions.csv. Data is never dropped without a
row here.

Every record carries a severity. An ``error`` blocks the affected row (or
file) from the governed dataset; a ``warning`` is reported but the row is
still published. ``kind`` names the category of exception so reports can
count them without parsing reasons.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import polars as pl

logger = logging.getLogger(__name__)

Severity = Literal["error", "warning"]

EXCEPTION_SCHEMA = {
    "file": pl.String,
    "row": pl.Int64,
    "column": pl.String,
    "value": pl.String,
    "kind": pl.String,
    "severity": pl.String,
    "reason": pl.String,
}


@dataclass(frozen=True)
class ExceptionRecord:
    """One thing Muster could not process, and why.

    ``row`` is the source row number (header is row 1); it is empty for
    file- or column-level exceptions. ``severity`` is ``error`` (the row is
    held out of the governed dataset) or ``warning`` (published, reported).
    """

    file: str
    reason: str
    row: int | None = None
    column: str | None = None
    value: str | None = None
    kind: str = "other"
    severity: Severity = "error"


def write_exceptions(records: Sequence[ExceptionRecord], path: Path) -> None:
    """Write records to CSV; an empty run still gets a header-only file."""
    frame = pl.DataFrame(
        [asdict(record) for record in records], schema=EXCEPTION_SCHEMA
    ).select(list(EXCEPTION_SCHEMA))
    frame.write_csv(path)
    logger.debug("wrote exceptions path=%s count=%d", path, len(records))


def count_by_severity(records: Sequence[ExceptionRecord]) -> dict[str, int]:
    """Count records per severity, always reporting both severities."""
    counts = {"error": 0, "warning": 0}
    for record in records:
        counts[record.severity] = counts.get(record.severity, 0) + 1
    return counts
