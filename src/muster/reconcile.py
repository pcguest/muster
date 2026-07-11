"""Reconcile duplicate keys across source files.

When the same key appears more than once, Muster never guesses. Rows whose
values agree are merged (losslessly, with a written warning). Rows whose
values conflict get a conflict exception listing each source's value, and are
held out of the governed dataset unless the configuration explicitly names a
survivorship strategy that can decide — ``newest_file`` (latest modified
source file wins) or ``priority_list`` (earliest listed file wins). The
``manual`` strategy always holds conflicts for review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

import polars as pl

from muster.config import Config
from muster.records import ExceptionRecord

logger = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    frame: pl.DataFrame
    exceptions: list[ExceptionRecord] = field(default_factory=list)
    rows_held: int = 0
    rows_superseded: int = 0
    conflicts_held: int = 0  # conflicting key groups left unresolved


def _key_display(row: Mapping[str, object], keys: Sequence[str]) -> str:
    return "/".join(str(row[key]) for key in keys)


def _cell(value: object) -> str:
    return "(empty)" if value is None else str(value)


def _sources_display(group: pl.DataFrame) -> str:
    seen: list[str] = []
    for name in group.get_column("_source_file").to_list():
        if name not in seen:
            seen.append(name)
    return ", ".join(seen)


def _conflicting_fields(group: pl.DataFrame, fields: Sequence[str]) -> list[str]:
    conflicting = []
    for name in fields:
        values = {v for v in group.get_column(name).to_list() if v is not None}
        if len(values) > 1:
            conflicting.append(name)
    return conflicting


def _merge_agreeing_rows(group: pl.DataFrame, fields: Sequence[str]) -> pl.DataFrame:
    """Coalesce rows that agree wherever both hold a value."""
    merged = group.head(1).clone()
    for name in fields:
        first = next((v for v in group.get_column(name).to_list() if v is not None), None)
        merged = merged.with_columns(
            pl.lit(first, dtype=group.schema[name]).alias(name)
        )
    files = "; ".join(dict.fromkeys(group.get_column("_source_file").to_list()))
    return merged.with_columns(pl.lit(files).alias("_source_file"))


def _pick_newest(group: pl.DataFrame, mtimes: Mapping[str, float]) -> tuple[int | None, str]:
    files = group.get_column("_source_file").to_list()
    if len(set(files)) == 1:
        return None, "conflicting rows come from one file; newest_file cannot decide"
    newest = max(mtimes.get(name, 0.0) for name in set(files))
    candidates = [i for i, name in enumerate(files) if mtimes.get(name, 0.0) == newest]
    if len(candidates) != 1:
        return None, "sources share a modification time; newest_file cannot decide"
    return candidates[0], f"resolved by newest_file: kept {files[candidates[0]]}"


def _pick_priority(
    group: pl.DataFrame, priority: Sequence[str]
) -> tuple[int | None, str]:
    def rank(name: str) -> int:
        for index, entry in enumerate(priority):
            if entry == name or entry == Path(name).name:
                return index
        return len(priority)

    files = group.get_column("_source_file").to_list()
    ranks = [rank(name) for name in files]
    best = min(ranks)
    if best == len(priority):
        return None, "no conflicting source appears in the priority list"
    candidates = [i for i, r in enumerate(ranks) if r == best]
    if len(candidates) != 1:
        return None, (
            f"priority list cannot decide between rows of {files[candidates[0]]}"
        )
    return candidates[0], f"resolved by priority_list: kept {files[candidates[0]]}"


def reconcile(
    frame: pl.DataFrame,
    config: Config,
    mtimes: Mapping[str, float] | None = None,
) -> ReconcileResult:
    """Deduplicate and reconcile ``frame`` on the configured key columns.

    ``frame`` holds validated rows with ``_source_file`` and ``_row``
    provenance; ``mtimes`` maps source file names to modification times for
    the ``newest_file`` strategy.
    """
    keys = config.validation.keys
    if not keys or frame.height == 0:
        return ReconcileResult(frame=frame)

    result = ReconcileResult(frame=frame)
    mtimes = mtimes or {}
    field_names = [spec.name for spec in config.fields]
    survivorship = config.validation.survivorship

    null_key = pl.any_horizontal([pl.col(key).is_null() for key in keys])
    for row in frame.filter(null_key).iter_rows(named=True):
        empty = next(key for key in keys if row[key] is None)
        result.exceptions.append(
            ExceptionRecord(
                file=row["_source_file"],
                row=int(row["_row"]),
                column=empty,
                kind="missing_key",
                severity="error",
                reason=f"empty key column '{empty}'; row held",
            )
        )
    result.rows_held += frame.filter(null_key).height

    # Rows whose key is unique pass straight through; only duplicated keys
    # are partitioned into groups, so group handling costs nothing on the
    # (overwhelmingly common) unique rows.
    working = frame.filter(~null_key)
    duplicated = working.select(d=pl.struct(keys).is_duplicated()).get_column("d")
    published: list[pl.DataFrame] = [working.filter(~duplicated)]
    for group in working.filter(duplicated).partition_by(keys, maintain_order=True):
        first = group.row(0, named=True)
        key_text = _key_display(first, keys)
        conflicting = _conflicting_fields(group, field_names)

        if not conflicting:
            published.append(_merge_agreeing_rows(group, field_names))
            result.rows_superseded += group.height - 1
            result.exceptions.append(
                ExceptionRecord(
                    file=_sources_display(group),
                    column=", ".join(keys),
                    value=key_text,
                    kind="duplicate_key",
                    severity="warning",
                    reason=(
                        f"key '{key_text}' appears in {group.height} rows with no "
                        "disagreement; merged into one"
                    ),
                )
            )
            continue

        if survivorship is None:
            survivor, note = None, "no survivorship strategy configured; rows held for review"
        elif survivorship.strategy == "manual":
            survivor, note = None, "survivorship strategy is manual; rows held for review"
        elif survivorship.strategy == "newest_file":
            survivor, note = _pick_newest(group, mtimes)
        else:
            survivor, note = _pick_priority(group, survivorship.priority)

        severity = "warning" if survivor is not None else "error"
        for name in conflicting:
            listing = " | ".join(
                f"{row['_source_file']} row {row['_row']}: {_cell(row[name])}"
                for row in group.iter_rows(named=True)
            )
            result.exceptions.append(
                ExceptionRecord(
                    file=_sources_display(group),
                    column=name,
                    value=listing,
                    kind="conflict",
                    severity=severity,  # type: ignore[arg-type]
                    reason=f"conflicting values for key '{key_text}'; {note}",
                )
            )
        if survivor is not None:
            published.append(group.slice(survivor, 1))
            result.rows_superseded += group.height - 1
        else:
            result.rows_held += group.height
            result.conflicts_held += 1

    result.frame = pl.concat(published, how="vertical")
    logger.info(
        "reconciled rows_in=%d rows_out=%d held=%d merged=%d conflicts_held=%d",
        frame.height,
        result.frame.height,
        result.rows_held,
        result.rows_superseded,
        result.conflicts_held,
    )
    return result
