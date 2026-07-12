"""Read-side data for the web interface, plus the append-only audit log.

Everything shown in the dashboard comes from artefacts the pipeline already
writes: run manifests (trends), the archived report data (latest run),
exceptions.csv and mapping-review.yaml. Exception resolutions never mutate
any of those — they append to ``runs/resolutions.jsonl``, an audit log of
who decided what and when, keyed by a stable exception fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from muster.config import Config
from muster.manifest import MANIFEST_NAME, RUNS_DIRECTORY, latest_manifest_of_kind
from muster.report import REPORT_DATA_NAME, RunReportData

logger = logging.getLogger(__name__)

RESOLUTIONS_FILE = "resolutions.jsonl"
RESOLUTION_ACTIONS = ("resolved", "dismissed")
MAX_NOTE_LENGTH = 500


@dataclass(frozen=True)
class ExceptionRow:
    """One exceptions.csv row with its stable fingerprint and resolution."""

    id: str
    file: str
    row: str
    column: str
    value: str
    kind: str
    severity: str
    reason: str
    resolution: str | None = None  # resolved | dismissed
    note: str | None = None
    resolved_at: str | None = None


@dataclass(frozen=True)
class TrendPoint:
    run_id: str
    finished_at: str
    rows_in: int
    rows_published: int
    errors: int
    warnings: int
    duration_seconds: float


def latest_report(root: Path) -> RunReportData | None:
    found = latest_manifest_of_kind(root / RUNS_DIRECTORY, "run")
    if found is None:
        return None
    data_path = found[0].parent / REPORT_DATA_NAME
    if not data_path.is_file():
        return None
    return RunReportData.from_json(data_path.read_text(encoding="utf-8"))


def run_trends(root: Path, limit: int = 30) -> list[TrendPoint]:
    """Totals across recent pipeline runs, oldest first, from the manifests."""
    runs_dir = root / RUNS_DIRECTORY
    points: list[TrendPoint] = []
    if not runs_dir.is_dir():
        return points
    for run_dir in sorted(runs_dir.iterdir()):
        manifest_path = run_dir / MANIFEST_NAME
        if not run_dir.is_dir() or not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind", "run") != "run":
            continue
        totals = manifest.get("totals", {})
        points.append(
            TrendPoint(
                run_id=str(manifest.get("run_id", run_dir.name)),
                finished_at=str(manifest.get("finished_at", "")),
                rows_in=int(totals.get("rows_in", 0)),
                rows_published=int(totals.get("rows_published", 0)),
                errors=int(totals.get("errors", 0)),
                warnings=int(totals.get("warnings", 0)),
                duration_seconds=float(manifest.get("duration_seconds", 0.0)),
            )
        )
    return points[-limit:]


def exception_fingerprint(
    run_id: str, file: str, row: str, column: str, kind: str, reason: str
) -> str:
    """A stable identifier for one exception within one run."""
    digest = hashlib.sha256(
        "|".join((run_id, file, row, column, kind, reason)).encode("utf-8")
    )
    return digest.hexdigest()[:16]


def resolutions_path(root: Path) -> Path:
    return root / RUNS_DIRECTORY / RESOLUTIONS_FILE


def load_resolutions(root: Path) -> dict[str, dict[str, str]]:
    """The latest resolution per exception id; later entries supersede."""
    path = resolutions_path(root)
    if not path.is_file():
        return {}
    latest: dict[str, dict[str, str]] = {}
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


def append_resolution(root: Path, exception_id: str, action: str, note: str) -> None:
    """Append one decision to the audit log; history is never rewritten."""
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "id": exception_id,
        "action": action,
        "note": note,
    }
    path = resolutions_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info("resolution recorded id=%s action=%s", exception_id, action)


def load_exceptions(root: Path, config: Config) -> list[ExceptionRow]:
    """The latest run's exceptions merged with their resolution state."""
    found = latest_manifest_of_kind(root / RUNS_DIRECTORY, "run")
    csv_path = root / config.output.directory / "exceptions.csv"
    if found is None or not csv_path.is_file():
        return []
    run_id = str(found[1].get("run_id", ""))
    frame = pl.read_csv(csv_path, infer_schema=False)
    resolutions = load_resolutions(root)
    rows: list[ExceptionRow] = []
    for record in frame.iter_rows(named=True):
        file = record.get("file") or ""
        row = record.get("row") or ""
        column = record.get("column") or ""
        kind = record.get("kind") or ""
        reason = record.get("reason") or ""
        exception_id = exception_fingerprint(run_id, file, row, column, kind, reason)
        resolution = resolutions.get(exception_id)
        rows.append(
            ExceptionRow(
                id=exception_id,
                file=file,
                row=row,
                column=column,
                value=record.get("value") or "",
                kind=kind,
                severity=record.get("severity") or "",
                reason=reason,
                resolution=resolution.get("action") if resolution else None,
                note=resolution.get("note") if resolution else None,
                resolved_at=resolution.get("at") if resolution else None,
            )
        )
    return rows
