"""Read-side data for the web interface.

Everything shown in the dashboard comes from artefacts the pipeline already
writes: run manifests (trends), the archived report data (latest run),
exceptions.csv and mapping-review.yaml. Exception decisions — resolve,
dismiss and correct — live in :mod:`muster.remediation` because the CLI and
the pipeline share them; they are re-exported here for the routes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from muster.manifest import MANIFEST_NAME, RUNS_DIRECTORY, latest_manifest_of_kind
from muster.remediation import (
    MAX_NOTE_LENGTH,
    MAX_VALUE_LENGTH,
    RESOLUTION_ACTIONS,
    RESOLUTIONS_FILE,
    ExceptionRow,
    append_resolution,
    check_correction,
    exception_fingerprint,
    load_exceptions,
    load_resolutions,
    resolutions_path,
)
from muster.report import REPORT_DATA_NAME, RunReportData

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_NOTE_LENGTH",
    "MAX_VALUE_LENGTH",
    "RESOLUTION_ACTIONS",
    "RESOLUTIONS_FILE",
    "ExceptionRow",
    "TrendPoint",
    "append_resolution",
    "check_correction",
    "exception_fingerprint",
    "latest_report",
    "load_exceptions",
    "load_resolutions",
    "resolutions_path",
    "run_trends",
]


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
