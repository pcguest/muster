"""Coerce string columns to canonical types with per-cell error capture.

Coercion is strict: a cell either parses cleanly or is reported as a failure.
Empty cells become nulls, not failures. Date parsing tries a fixed, ordered
list of formats — day-first before month-first, so a month-first date is only
accepted when no day-first reading is possible. The order is deterministic
and documented here; nothing is inferred per file.

All-numeric formats are parsed with Polars' vectorised parsers in non-strict
mode, one expression per format, coalesced in order: a malformed cell becomes
a null, never a crash. Month-name formats (``%b``/``%B``) are deliberately
NOT vectorised — Polars' fast-path parser for them does unchecked slicing
keyed off the first row and panics on crafted or merely unlucky value mixes,
and input files are untrusted. Cells the vectorised formats cannot parse fall
back to per-cell ``datetime.strptime`` for the month-name formats only, which
preserves the documented format order because a month-name reading can never
overlap an all-numeric one.
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


def _chrono(fmt: str) -> str:
    """Translate a Python strptime format to its chrono (Polars) equivalent."""
    return fmt.replace(".%f", "%.f")


def _has_month_name(fmt: str) -> bool:
    return "%b" in fmt or "%B" in fmt


_VECTOR_DATE_FORMATS = [f for f in DATE_FORMATS if not _has_month_name(f)]
_FALLBACK_DATE_FORMATS = [f for f in DATE_FORMATS if _has_month_name(f)]


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


def _date_expr(cleaned: pl.Expr) -> pl.Expr:
    """The first accepted all-numeric date reading, in documented order."""
    readings = [
        cleaned.str.to_date(_chrono(fmt), strict=False)
        for fmt in _VECTOR_DATE_FORMATS
    ] + [
        cleaned.str.to_datetime(_chrono(fmt), strict=False, time_unit="us").dt.date()
        for fmt in DATETIME_FORMATS
    ]
    return pl.coalesce(readings)


def _datetime_expr(cleaned: pl.Expr) -> pl.Expr:
    """The first accepted datetime reading; date-only formats give midnight."""
    readings = [
        cleaned.str.to_datetime(_chrono(fmt), strict=False, time_unit="us")
        for fmt in DATETIME_FORMATS
    ] + [
        cleaned.str.to_date(_chrono(fmt), strict=False).cast(pl.Datetime("us"))
        for fmt in _VECTOR_DATE_FORMATS
    ]
    return pl.coalesce(readings)


def _month_name_fallback(value: str) -> datetime | None:
    for fmt in _FALLBACK_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _apply_month_name_fallback(
    coerced: pl.Series, cleaned: pl.Series, field_type: FieldType
) -> pl.Series:
    """Per-cell parse of month-name dates for cells left null by the
    vectorised formats. Runs only on those cells, so a clean numeric column
    pays nothing."""
    unresolved = (coerced.is_null() & cleaned.is_not_null()).arg_true()
    if unresolved.is_empty():
        return coerced
    parsed = [_month_name_fallback(cleaned[i]) for i in unresolved.to_list()]
    values: list[date | None] | list[datetime | None]
    if field_type == "date":
        values = [p.date() if p is not None else None for p in parsed]
    else:
        values = parsed
    return coerced.scatter(
        unresolved, pl.Series(values, dtype=TYPE_DTYPES[field_type])
    )


def coerce_series(values: pl.Series, field_type: FieldType) -> tuple[pl.Series, pl.Series]:
    """Coerce a string series to ``field_type``.

    Returns ``(coerced, failed)`` where ``failed`` is a boolean series marking
    cells that held a value but could not be coerced. Failed cells are null in
    ``coerced``; the originals belong in the exception log, not the dataset.
    """
    frame = pl.DataFrame({"value": values.cast(pl.String)})
    cleaned = pl.col("value").str.strip_chars()
    empty = cleaned.is_null() | (cleaned == pl.lit(""))

    if field_type == "string":
        coerced = pl.when(empty).then(pl.lit(None, dtype=pl.String)).otherwise(cleaned)
    elif field_type == "integer":
        coerced = _numeric_expr(cleaned, pl.Int64())
    elif field_type == "float":
        coerced = _numeric_expr(cleaned, pl.Float64())
    elif field_type == "boolean":
        coerced = _boolean_expr(cleaned)
    elif field_type == "date":
        coerced = _date_expr(cleaned)
    elif field_type == "datetime":
        coerced = _datetime_expr(cleaned)
    else:  # defensive: config validation should make this unreachable
        raise ValueError(f"unknown field type '{field_type}'")
    result = frame.select(
        cleaned=pl.when(empty).then(pl.lit(None, dtype=pl.String)).otherwise(cleaned),
        coerced=coerced,
    )
    cleaned_series = result.get_column("cleaned")
    coerced_series = result.get_column("coerced").rename(values.name)
    if field_type in ("date", "datetime"):
        coerced_series = _apply_month_name_fallback(
            coerced_series, cleaned_series, field_type
        )
    failed_series = coerced_series.is_null() & cleaned_series.is_not_null()
    failures = int(failed_series.sum())
    if failures:
        logger.debug(
            "coercion column=%r type=%s failures=%d", values.name, field_type, failures
        )
    return coerced_series, failed_series
