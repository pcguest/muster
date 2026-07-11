"""Profile a folder of spreadsheets before consolidating them.

For each CSV or XLSX file: the columns found, an inferred type per column,
the row count, and format inconsistencies worth knowing about before a run —
mixed date formats, thousands separators, and case variants of the same
value.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from muster.coercion import DATE_FORMATS, DATETIME_FORMATS
from muster.readers import ROW_COLUMN, read_table
from muster.security import SUPPORTED_SUFFIXES, SecurityError, ensure_size_within, ensure_within

logger = logging.getLogger(__name__)

# Cap the values examined per column so profiling stays quick on big files.
_SAMPLE_LIMIT = 1000

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?\d+\.\d+$")
_THOUSANDS_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_BOOL_VALUES = frozenset({"true", "false", "t", "f", "yes", "no", "y", "n"})

# The same formats the pipeline's coercion accepts, so profiling reports
# match run behaviour.
_DATE_FORMATS = DATE_FORMATS + DATETIME_FORMATS


@dataclass
class ColumnProfile:
    name: str
    inferred_type: str
    non_empty: int
    issues: list[str] = field(default_factory=list)


@dataclass
class FileProfile:
    file: str
    rows: int = 0
    columns: list[ColumnProfile] = field(default_factory=list)
    error: str | None = None


def _first_date_format(value: str) -> str | None:
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(value, fmt)
        except ValueError:
            continue
        return fmt
    return None


def _profile_column(name: str, values: list[str | None]) -> ColumnProfile:
    sample = [v.strip() for v in values if v is not None and v.strip()]
    non_empty = len(sample)
    sample = sample[:_SAMPLE_LIMIT]
    if not sample:
        return ColumnProfile(name, "string", 0, ["no values"])

    issues: list[str] = []
    ints = floats = thousands = bools = 0
    date_formats: set[str] = set()
    dates = 0
    for value in sample:
        if _INT_RE.match(value):
            ints += 1
            continue
        if _FLOAT_RE.match(value):
            floats += 1
            continue
        if _THOUSANDS_RE.match(value):
            thousands += 1
            continue
        if value.lower() in _BOOL_VALUES:
            bools += 1
            continue
        fmt = _first_date_format(value)
        if fmt is not None:
            dates += 1
            date_formats.add(fmt)

    total = len(sample)
    if ints == total:
        inferred = "integer"
    elif ints + floats + thousands == total:
        inferred = "float"
    elif bools == total:
        inferred = "boolean"
    elif dates == total:
        inferred = "date"
    else:
        inferred = "string"

    if len(date_formats) > 1:
        issues.append("mixed date formats: " + ", ".join(sorted(date_formats)))
    if thousands:
        issues.append(f"thousands separators in {thousands} value(s)")

    if inferred in ("string", "boolean"):
        variants: dict[str, set[str]] = {}
        for value in sample:
            variants.setdefault(value.lower(), set()).add(value)
        cased = {k: v for k, v in variants.items() if len(v) > 1}
        if cased:
            example = sorted(next(iter(cased.values())))
            issues.append(
                f"case variants of {len(cased)} value(s), e.g. " + " / ".join(example)
            )

    return ColumnProfile(name, inferred, non_empty, issues)


def profile_file(path: Path, max_file_size_mb: int) -> FileProfile:
    """Profile one spreadsheet; failures are reported, not raised."""
    profile = FileProfile(file=path.name)
    try:
        ensure_size_within(path, max_file_size_mb)
        frame = read_table(path)
    except (SecurityError, RuntimeError) as exc:
        profile.error = str(exc)
        logger.warning("profile skipped file=%s reason=%s", path.name, exc)
        return profile
    profile.rows = frame.height
    for name in frame.columns:
        if name == ROW_COLUMN:
            continue
        profile.columns.append(_profile_column(name, frame.get_column(name).to_list()))
    logger.debug("profiled file=%s rows=%d columns=%d", path.name, profile.rows, len(profile.columns))
    return profile


def profile_folder(folder: Path, max_file_size_mb: int = 100) -> list[FileProfile]:
    """Profile every supported spreadsheet directly inside ``folder``."""
    if not folder.is_dir():
        raise FileNotFoundError(f"not a folder: {folder}")
    root = folder.resolve()
    files = sorted(
        entry
        for entry in root.iterdir()
        if entry.is_file() and entry.suffix.lower() in SUPPORTED_SUFFIXES
    )
    profiles = []
    for path in files:
        ensure_within(path, root)
        profiles.append(profile_file(path, max_file_size_mb))
    return profiles
