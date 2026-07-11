"""Coerce string columns to canonical types with per-cell error capture.

Coercion is strict: a cell either parses cleanly or is reported as a failure.
Empty cells become nulls, not failures. Date parsing tries a fixed, ordered
list of formats — day-first before month-first, so a month-first date is only
accepted when no day-first reading is possible. The order is deterministic
and documented here; nothing is inferred per file.

Dates are parsed per cell with Python's ``datetime.strptime`` rather than the
vectorised parser: Polars' native strptime can panic on malformed strings,
and input files are untrusted, so a bad cell must become an exception record,
never a crash.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import polars as pl

from muster.config import FieldType

logger = logging.getLogger(__name__)

TYPE_DTYPES: dict[str, pl.DataType] = {
    "string": pl.String(),
    "integer": pl.Int64(),
    "float": pl.Float64(),
    "boolean": pl.Boolean(),
    "date": pl.Date(),
    "datetime": pl.Datetime("us"),
}

# Numbers with grouping commas, e.g. "1,204.50"; the commas are stripped
# before casting. Anything else is cast as-is.
_THOUSANDS_PATTERN = r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$"

_TRUE_VALUES = ["true", "t", "yes", "y", "1"]
_FALSE_VALUES = ["false", "f", "no", "n", "0"]

DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y/%m/%d",
]

DATETIME_FORMATS = [
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%d/%m/%Y %H:%M",
]


def _numeric_expr(cleaned: pl.Expr, dtype: pl.DataType) -> pl.Expr:
    without_grouping = (
        pl.when(cleaned.str.contains(_THOUSANDS_PATTERN))
        .then(cleaned.str.replace_all(",", "", literal=True))
        .otherwise(cleaned)
    )
    return without_grouping.cast(dtype, strict=False)


def _boolean_expr(cleaned: pl.Expr) -> pl.Expr:
    lowered = cleaned.str.to_lowercase()
    return (
        pl.when(lowered.is_in(_TRUE_VALUES))
        .then(pl.lit(True))
        .when(lowered.is_in(_FALSE_VALUES))
        .then(pl.lit(False))
        .otherwise(pl.lit(None, dtype=pl.Boolean))
    )


def _parse_date(value: str) -> date | None:
    for fmt in DATE_FORMATS + DATETIME_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: str) -> datetime | None:
    for fmt in DATETIME_FORMATS + DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def coerce_series(values: pl.Series, field_type: FieldType) -> tuple[pl.Series, pl.Series]:
    """Coerce a string series to ``field_type``.

    Returns ``(coerced, failed)`` where ``failed`` is a boolean series marking
    cells that held a value but could not be coerced. Failed cells are null in
    ``coerced``; the originals belong in the exception log, not the dataset.
    """
    frame = pl.DataFrame({"value": values.cast(pl.String)})
    cleaned = pl.col("value").str.strip_chars()
    empty = cleaned.is_null() | (cleaned == pl.lit(""))

    if field_type in ("date", "datetime"):
        parser = _parse_date if field_type == "date" else _parse_datetime
        cleaned_values = frame.select(v=cleaned).get_column("v").to_list()
        parsed = [parser(v) if v else None for v in cleaned_values]
        coerced_series = pl.Series(values.name, parsed, dtype=TYPE_DTYPES[field_type])
        empty_series = frame.select(e=empty).get_column("e")
        failed_series = coerced_series.is_null() & ~empty_series
    else:
        if field_type == "string":
            coerced = pl.when(empty).then(pl.lit(None, dtype=pl.String)).otherwise(cleaned)
        elif field_type == "integer":
            coerced = _numeric_expr(cleaned, pl.Int64())
        elif field_type == "float":
            coerced = _numeric_expr(cleaned, pl.Float64())
        elif field_type == "boolean":
            coerced = _boolean_expr(cleaned)
        else:  # defensive: config validation should make this unreachable
            raise ValueError(f"unknown field type '{field_type}'")
        result = frame.select(
            coerced=coerced,
            failed=coerced.is_null() & ~empty,
        )
        coerced_series = result.get_column("coerced").rename(values.name)
        failed_series = result.get_column("failed")
    failures = int(failed_series.sum())
    if failures:
        logger.debug(
            "coercion column=%r type=%s failures=%d", values.name, field_type, failures
        )
    return coerced_series, failed_series
