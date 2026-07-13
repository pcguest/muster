"""Exception remediation: corrected rows rejoin the governed dataset.

A held row is not a dead end. A person records a correction against the
exception's fingerprint — a stable digest of the defect itself (file, row,
column, value, kind, reason), so the same defect carries the same identity
run after run. Corrections append to ``runs/resolutions.jsonl`` alongside
the existing resolve/dismiss decisions; history is never mutated, and a
re-correction is simply a newer record superseding the old one.

On the next run the pipeline re-derives every exception from the sources as
usual, then applies any recorded correction whose fingerprint matches: the
corrected values are coerced and the whole row is re-validated against the
full rule set. A row only rejoins the governed dataset when every error on
it is corrected and re-validation is clean — a correction that still fails
the rules leaves the row held, with a new exception saying exactly why. The
run manifest records how many rows entered this way and which resolution
ids put them there.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from muster.coercion import TYPE_DTYPES, coerce_series
from muster.config import Config
from muster.manifest import RUNS_DIRECTORY, latest_manifest_of_kind
from muster.records import ExceptionRecord

logger = logging.getLogger(__name__)

RESOLUTIONS_FILE = "resolutions.jsonl"
RESOLUTION_ACTIONS = ("resolved", "dismissed", "corrected")
MAX_NOTE_LENGTH = 500
MAX_VALUE_LENGTH = 500

# The provenance columns validate_frame expects; they belong to the
# pipeline's internal schema and are spelled out here to avoid a circular
# import (the pipeline imports this module).
_SOURCE_FILE_COLUMN = "_source_file"
_ROW_COLUMN = "_row"

# Coercion exceptions name their canonical field in the reason with exactly
# this phrasing (see pipeline._coerce_chunk); it is ours to parse.
_FIELD_IN_REASON = re.compile(r"for field '([^']+)'")


@dataclass(frozen=True)
class ExceptionRow:
    """One exceptions.csv row with its stable fingerprint and resolution.

    ``field`` is the canonical field a correction would target, when one can
    be derived; ``corrected_values`` echoes a recorded correction awaiting
    the next run.
    """

    id: str
    file: str
    row: str
    column: str
    value: str
    kind: str
    severity: str
    reason: str
    resolution: str | None = None  # resolved | dismissed | corrected
    note: str | None = None
    resolved_at: str | None = None
    field: str | None = None
    corrected_values: dict[str, str] | None = None


@dataclass(frozen=True)
class Correction:
    """The latest recorded correction for one exception fingerprint."""

    id: str
    at: str
    note: str
    values: dict[str, str]


@dataclass(frozen=True)
class RemediationOutcome:
    """What applying recorded corrections did to one run."""

    frame: pl.DataFrame  # recovered rows, in the pipeline's internal schema
    exceptions: list[ExceptionRecord]  # the full, updated exception list
    rows_remediated: int
    resolution_ids: list[str] = field(default_factory=list)


def exception_fingerprint(
    file: str, row: str, column: str, value: str, kind: str, reason: str
) -> str:
    """A stable identifier for one defect, unchanged from run to run.

    Deliberately excludes the run id: the same source defect must carry the
    same fingerprint on every run, so a recorded correction keeps applying
    until the source itself changes (a changed value changes the digest and
    voids the correction).
    """
    digest = hashlib.sha256(
        "|".join((file, row, column, value, kind, reason)).encode("utf-8")
    )
    return digest.hexdigest()[:16]


def record_fingerprint(record: ExceptionRecord) -> str:
    """The fingerprint of an in-memory exception record."""
    return exception_fingerprint(
        record.file,
        "" if record.row is None else str(record.row),
        record.column or "",
        record.value or "",
        record.kind,
        record.reason,
    )


def resolutions_path(root: Path) -> Path:
    return root / RUNS_DIRECTORY / RESOLUTIONS_FILE


def load_resolutions(root: Path) -> dict[str, dict[str, Any]]:
    """The latest resolution per exception id; later entries supersede."""
    path = resolutions_path(root)
    if not path.is_file():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("skipping malformed resolution line")
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            latest[entry["id"]] = entry
    return latest


def append_resolution(
    root: Path,
    exception_id: str,
    action: str,
    note: str,
    corrected_values: Mapping[str, str] | None = None,
) -> None:
    """Append one decision to the audit log; history is never rewritten."""
    entry: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "id": exception_id,
        "action": action,
        "note": note,
    }
    if corrected_values:
        entry["corrected_values"] = dict(corrected_values)
    path = resolutions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("resolution recorded id=%s action=%s", exception_id, action)


def load_corrections(root: Path) -> dict[str, Correction]:
    """Fingerprints whose latest resolution is a correction with values.

    Records without ``corrected_values`` (every pre-1.2 record) are simply
    not corrections; a later resolve or dismiss on the same fingerprint
    supersedes an earlier correction and withdraws it.
    """
    corrections: dict[str, Correction] = {}
    for exception_id, entry in load_resolutions(root).items():
        values = entry.get("corrected_values")
        if entry.get("action") != "corrected" or not isinstance(values, dict):
            continue
        corrections[exception_id] = Correction(
            id=exception_id,
            at=str(entry.get("at", "")),
            note=str(entry.get("note", "")),
            values={str(k): str(v) for k, v in values.items()},
        )
    return corrections


def correction_field(column: str, reason: str, config: Config) -> str | None:
    """The canonical field a correction of this exception would set.

    Rule violations name the field in ``column``; coercion failures name the
    source column there, with the field in the reason's fixed phrasing.
    """
    names = {spec.name for spec in config.fields}
    if column in names:
        return column
    match = _FIELD_IN_REASON.search(reason)
    if match and match.group(1) in names:
        return match.group(1)
    return None


def _one_row_frame(config: Config, coerced: Mapping[str, pl.Series]) -> pl.DataFrame:
    columns = [
        pl.Series(_SOURCE_FILE_COLUMN, ["correction"], dtype=pl.String),
        pl.Series(_ROW_COLUMN, [0], dtype=pl.Int64),
    ]
    for spec in config.fields:
        series = coerced.get(spec.name)
        if series is None:
            series = pl.Series(spec.name, [None], dtype=TYPE_DTYPES[spec.type])
        columns.append(series.rename(spec.name))
    return pl.DataFrame(columns)


def check_correction(config: Config, values: Mapping[str, str]) -> list[str]:
    """Validate proposed values exactly as the pipeline will apply them.

    Returns precise failure messages, empty when the correction is
    acceptable: every field exists, every value coerces to its declared
    type, and no error-severity field rule rejects it. Cross-field rules
    need the whole row and run when the correction is applied.
    """
    from muster.rules import validate_frame

    if not values:
        return ["no corrected values given"]
    specs = {spec.name: spec for spec in config.fields}
    failures: list[str] = []
    coerced: dict[str, pl.Series] = {}
    for name, raw in values.items():
        spec = specs.get(name)
        if spec is None:
            failures.append(f"unknown field '{name}'")
            continue
        if len(raw) > MAX_VALUE_LENGTH:
            failures.append(f"value for field '{name}' is too long")
            continue
        series, failed = coerce_series(pl.Series(name, [raw]), spec.type)
        if bool(failed[0]):
            failures.append(
                f"'{raw}' cannot be coerced to {spec.type} for field '{name}'"
            )
            continue
        coerced[name] = series
    if failures:
        return failures
    for violation in validate_frame(_one_row_frame(config, coerced), config):
        if violation.severity == "error" and violation.column in values:
            failures.append(violation.reason)
    return failures


def apply_corrections(
    all_rows: pl.DataFrame,
    exceptions: Sequence[ExceptionRecord],
    config: Config,
    corrections: Mapping[str, Correction],
) -> RemediationOutcome:
    """Apply recorded corrections to this run's held rows.

    A row is recovered only when every error exception on it has a matching
    correction, the corrected values coerce, and re-validation against the
    full rule set is clean. Recovered rows have their original error records
    replaced by one ``remediated`` warning naming the resolutions used; any
    other outcome leaves the row held with a ``remediation_failed`` error
    saying why.
    """
    from muster.rules import validate_frame

    outcome_exceptions = list(exceptions)
    if not corrections:
        return RemediationOutcome(
            frame=all_rows.clear(), exceptions=outcome_exceptions, rows_remediated=0
        )

    by_row: dict[tuple[str, int], list[ExceptionRecord]] = {}
    for record in exceptions:
        if record.severity == "error" and record.row is not None:
            by_row.setdefault((record.file, record.row), []).append(record)

    specs = {spec.name: spec for spec in config.fields}
    recovered: list[pl.DataFrame] = []
    replaced: set[int] = set()
    added: list[ExceptionRecord] = []
    applied_ids: set[str] = set()
    rows_remediated = 0

    for (file, row), records in sorted(by_row.items()):
        matched = {
            fid: corrections[fid]
            for fid in (record_fingerprint(r) for r in records)
            if fid in corrections
        }
        if not matched:
            continue
        ids_text = ", ".join(sorted(matched))
        if len(matched) < len(records):
            added.append(
                ExceptionRecord(
                    file=file,
                    row=row,
                    kind="remediation_failed",
                    reason=(
                        f"correction(s) {ids_text} cover only some of this "
                        "row's errors; row stays held"
                    ),
                )
            )
            continue

        merged: dict[str, str] = {}
        for fid in sorted(matched):
            merged.update(matched[fid].values)

        source_row = all_rows.filter(
            (pl.col(_SOURCE_FILE_COLUMN) == file) & (pl.col(_ROW_COLUMN) == row)
        )
        if source_row.height != 1:
            added.append(
                ExceptionRecord(
                    file=file,
                    row=row,
                    kind="remediation_failed",
                    reason=(
                        f"correction(s) {ids_text} could not be applied: "
                        "source row not found in this run"
                    ),
                )
            )
            continue

        problems: list[str] = []
        updates: dict[str, pl.Series] = {}
        for name in sorted(merged):
            spec = specs.get(name)
            if spec is None:
                problems.append(f"unknown field '{name}'")
                continue
            series, failed = coerce_series(pl.Series(name, [merged[name]]), spec.type)
            if bool(failed[0]):
                problems.append(
                    f"'{merged[name]}' cannot be coerced to {spec.type} "
                    f"for field '{name}'"
                )
                continue
            updates[name] = series
        if problems:
            added.append(
                ExceptionRecord(
                    file=file,
                    row=row,
                    kind="remediation_failed",
                    reason=(
                        f"correction(s) {ids_text} could not be applied: "
                        + "; ".join(problems)
                    ),
                )
            )
            continue

        corrected = source_row.with_columns(
            *(series.rename(name) for name, series in updates.items())
        )
        violations = validate_frame(corrected, config)
        blocking = [v for v in violations if v.severity == "error"]
        if blocking:
            added.append(
                ExceptionRecord(
                    file=file,
                    row=row,
                    column=blocking[0].column,
                    value=blocking[0].value,
                    kind="remediation_failed",
                    reason=(
                        f"correction(s) {ids_text} still fail(s) validation: "
                        + blocking[0].reason
                    ),
                )
            )
            continue

        recovered.append(corrected)
        replaced.update(id(record) for record in records)
        added.extend(violations)  # re-validation warnings are still reported
        summary = ", ".join(f"{name}={merged[name]}" for name in sorted(merged))
        added.append(
            ExceptionRecord(
                file=file,
                row=row,
                column=", ".join(sorted(merged)),
                value=summary,
                kind="remediated",
                severity="warning",
                reason=(
                    f"held value(s) corrected via resolution(s) {ids_text}; "
                    "re-validated and published"
                ),
            )
        )
        applied_ids.update(matched)
        rows_remediated += 1

    outcome_exceptions = [
        record for record in outcome_exceptions if id(record) not in replaced
    ] + added
    frame = pl.concat(recovered, how="vertical") if recovered else all_rows.clear()
    if rows_remediated:
        logger.info(
            "remediation applied rows=%d resolutions=%d",
            rows_remediated,
            len(applied_ids),
        )
    return RemediationOutcome(
        frame=frame,
        exceptions=outcome_exceptions,
        rows_remediated=rows_remediated,
        resolution_ids=sorted(applied_ids),
    )


def load_exceptions(root: Path, config: Config) -> list[ExceptionRow]:
    """The latest run's exceptions merged with their resolution state."""
    found = latest_manifest_of_kind(root / RUNS_DIRECTORY, "run")
    csv_path = root / config.output.directory / "exceptions.csv"
    if found is None or not csv_path.is_file():
        return []
    frame = pl.read_csv(csv_path, infer_schema=False)
    resolutions = load_resolutions(root)
    rows: list[ExceptionRow] = []
    for record in frame.iter_rows(named=True):
        file = record.get("file") or ""
        row = record.get("row") or ""
        column = record.get("column") or ""
        value = record.get("value") or ""
        kind = record.get("kind") or ""
        severity = record.get("severity") or ""
        reason = record.get("reason") or ""
        exception_id = exception_fingerprint(file, row, column, value, kind, reason)
        resolution = resolutions.get(exception_id)
        corrected_values = None
        if resolution and isinstance(resolution.get("corrected_values"), dict):
            corrected_values = {
                str(k): str(v) for k, v in resolution["corrected_values"].items()
            }
        target = (
            correction_field(column, reason, config)
            if severity == "error" and row
            else None
        )
        rows.append(
            ExceptionRow(
                id=exception_id,
                file=file,
                row=row,
                column=column,
                value=value,
                kind=kind,
                severity=severity,
                reason=reason,
                resolution=resolution.get("action") if resolution else None,
                note=resolution.get("note") if resolution else None,
                resolved_at=resolution.get("at") if resolution else None,
                field=target,
                corrected_values=corrected_values,
            )
        )
    return rows
