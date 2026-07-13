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
from datetime import datetime
from pathlib import Path
from typing import Any

from muster.config import Config
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
from muster.scheduler import SchedulerError, daemon_pid, read_schedule

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_NOTE_LENGTH",
    "MAX_VALUE_LENGTH",
    "RESOLUTION_ACTIONS",
    "RESOLUTIONS_FILE",
    "ExceptionRow",
    "AutomationStatus",
    "PublishOutcome",
    "PublishTarget",
    "TrendPoint",
    "append_resolution",
    "automation_status",
    "check_correction",
    "configured_targets",
    "exception_fingerprint",
    "latest_publish",
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
    rows_held: int
    errors: int
    warnings: int
    duration_seconds: float

    @property
    def quality_pct(self) -> float | None:
        if not self.rows_in:
            return None
        return round(100 * self.rows_published / self.rows_in, 1)


@dataclass(frozen=True)
class PublishTarget:
    name: str
    type: str
    destination: str
    key_columns: tuple[str, ...]


@dataclass(frozen=True)
class PublishOutcome:
    finished_at: str
    target: str
    type: str
    destination: str
    source_run: str
    rows: int
    rows_sent: int
    rows_failed: int
    outcome: str
    forced: bool
    duration_seconds: float


@dataclass(frozen=True)
class AutomationStatus:
    schedule: str | None
    next_run: str | None
    daemon_pid: int | None
    error: str | None = None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
                rows_in=_as_int(totals.get("rows_in", 0)),
                rows_published=_as_int(totals.get("rows_published", 0)),
                rows_held=_as_int(totals.get("rows_held", 0)),
                errors=_as_int(totals.get("errors", 0)),
                warnings=_as_int(totals.get("warnings", 0)),
                duration_seconds=_as_float(manifest.get("duration_seconds", 0.0)),
            )
        )
    return points[-limit:]


def configured_targets(config: Config) -> list[PublishTarget]:
    """Read-only descriptions of configured targets, never resolved secrets."""
    targets: list[PublishTarget] = []
    for name, target in sorted(config.targets.items()):
        if target.type == "sqlite":
            destination = f"{target.path} / {target.table}"
        elif target.type == "postgres":
            destination = f"table {target.table}"
        elif target.type == "rest":
            destination = target.url
        else:
            destination = f"{target.object} via {target.login_url}"
        targets.append(
            PublishTarget(
                name=name,
                type=target.type,
                destination=destination,
                key_columns=tuple(config.resolved_key_columns(target)),
            )
        )
    return targets


def latest_publish(root: Path) -> PublishOutcome | None:
    """The newest publish outcome from the manifest chain, if one exists."""
    found = latest_manifest_of_kind(root / RUNS_DIRECTORY, "publish")
    if found is None:
        return None
    manifest = found[1]
    publish = manifest.get("publish", {})
    if not isinstance(publish, dict):
        publish = {}
    return PublishOutcome(
        finished_at=str(manifest.get("finished_at", "")),
        target=str(publish.get("target", "unknown")),
        type=str(publish.get("type", "unknown")),
        destination=str(publish.get("destination", "not recorded")),
        source_run=str(publish.get("source_run", "unknown")),
        rows=_as_int(publish.get("rows", 0)),
        rows_sent=_as_int(publish.get("rows_sent", 0)),
        rows_failed=_as_int(publish.get("rows_failed", 0)),
        outcome=str(publish.get("outcome", "unknown")),
        forced=bool(publish.get("forced", False)),
        duration_seconds=_as_float(manifest.get("duration_seconds", 0.0)),
    )


def automation_status(root: Path) -> AutomationStatus:
    """Configured schedule and current daemon state, without mutating either."""
    pid = daemon_pid(root / RUNS_DIRECTORY)
    try:
        expression = read_schedule(root)
    except SchedulerError as exc:
        if not (root / "muster.schedule").is_file():
            return AutomationStatus(None, None, pid)
        return AutomationStatus(None, None, pid, str(exc))
    next_run = expression.next_after(datetime.now().astimezone()).isoformat(
        sep=" ", timespec="minutes"
    )
    return AutomationStatus(expression.raw, next_run, pid)
