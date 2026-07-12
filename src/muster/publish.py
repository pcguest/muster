"""Publishing the governed dataset to configured targets.

``muster publish [target]`` sends the *latest governed dataset* — the output
of the most recent pipeline run — to one target from ``targets:`` in
muster.yaml. Three protections apply, in order:

- **Integrity**: the dataset on disk must hash to exactly what the latest
  run's manifest recorded. A stale or tampered file is refused outright;
  --force does not override this.
- **Quality**: a run that recorded error-severity exceptions is refused
  unless --force is given, and a forced publish is recorded loudly in the
  manifest chain.
- **Audit**: every publish (including failed and forced ones) appends its
  own manifest to the tamper-evident chain: target, destination, row
  counts, duration, outcome. Dry runs write nothing at all.

Per-record failures from a target land in ``publish-exceptions.csv`` next to
the dataset, in the same schema as run exceptions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from muster.config import Config, TargetConfig
from muster.credentials import SecretError, redact_text
from muster.manifest import (
    RUNS_DIRECTORY,
    latest_manifest_of_kind,
    sha256_file,
    write_publish_manifest,
)
from muster.records import ExceptionRecord, write_exceptions
from muster.targets import Target, TargetError, build_target

logger = logging.getLogger(__name__)

PUBLISH_EXCEPTIONS_NAME = "publish-exceptions.csv"


class PublishError(RuntimeError):
    """Raised when a publish cannot proceed or fails outright."""

    def __init__(self, message: str) -> None:
        super().__init__(redact_text(message))


@dataclass
class PublishReport:
    """What one 'muster publish' invocation did (or would do)."""

    target: str
    target_type: str
    destination: str
    source_run: str
    rows: int
    rows_sent: int = 0
    rows_failed: int = 0
    outcome: str = "dry-run"  # dry-run | published | partial | failed
    forced: bool = False
    duration_seconds: float = 0.0
    plan: list[str] = field(default_factory=list)
    manifest_path: Path | None = None
    exceptions_csv: Path | None = None


def select_target(config: Config, name: str | None) -> tuple[str, TargetConfig]:
    """Resolve which configured target to publish to."""
    if not config.targets:
        raise PublishError(
            "no publish targets configured: add a 'targets:' section to "
            "muster.yaml (see docs/CONNECTORS.md)"
        )
    if name is None:
        if len(config.targets) == 1:
            return next(iter(config.targets.items()))
        raise PublishError(
            "several targets are configured; name one of: "
            + ", ".join(sorted(config.targets))
        )
    if name not in config.targets:
        raise PublishError(
            f"no target named '{name}'; configured targets: "
            + ", ".join(sorted(config.targets))
        )
    return name, config.targets[name]


def _load_governed_dataset(
    config: Config, root: Path
) -> tuple[pl.DataFrame, dict, int]:
    """The latest run's dataset, verified against its manifest.

    Returns the frame, the run manifest, and the run's error count.
    """
    found = latest_manifest_of_kind(root / RUNS_DIRECTORY, "run")
    if found is None:
        raise PublishError("no completed run to publish; run 'muster run' first")
    _, manifest = found
    dataset = (
        root / config.output.directory / f"{config.output.dataset_name}.parquet"
    )
    if not dataset.is_file():
        raise PublishError(
            f"governed dataset not found at {dataset}; run 'muster run' first"
        )
    recorded = manifest.get("outputs", {}).get(dataset.name, {}).get("sha256")
    actual = sha256_file(dataset)
    if recorded != actual:
        raise PublishError(
            f"{dataset.name} does not match the latest run manifest "
            f"(run {manifest.get('run_id')}): the file is stale or has been "
            "altered since the run. Rerun 'muster run' before publishing."
        )
    errors = int(manifest.get("totals", {}).get("errors", 0))
    return pl.read_parquet(dataset), manifest, errors


def _failure_records(
    report: PublishReport, target: Target, failures
) -> list[ExceptionRecord]:
    key_column = ", ".join(target.keys) if target.keys else None
    return [
        ExceptionRecord(
            file=f"target:{report.target}",
            column=key_column,
            value=failure.key or None,
            reason=redact_text(f"{failure.code}: {failure.message}"),
            kind="publish_failed",
        )
        for failure in failures
    ]


def publish_dataset(
    config: Config,
    root: Path,
    target_name: str | None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> PublishReport:
    """Publish the latest governed dataset to one configured target."""
    root = root.resolve()
    name, spec = select_target(config, target_name)
    frame, run_manifest, run_errors = _load_governed_dataset(config, root)
    if run_errors and not force and not dry_run:
        raise PublishError(
            f"refusing to publish: the latest run ({run_manifest.get('run_id')}) "
            f"recorded {run_errors} error-severity exception(s), so the governed "
            "dataset is incomplete. Fix the sources and rerun, or pass --force "
            "to publish anyway (the override is recorded in the manifest chain)."
        )

    target = build_target(name, spec, config, root)
    report = PublishReport(
        target=name,
        target_type=spec.type,
        destination=target.describe(),
        source_run=str(run_manifest.get("run_id")),
        rows=frame.height,
        forced=bool(force and run_errors),
    )

    if dry_run:
        report.plan = [redact_text(line) for line in target.plan(frame)]
        if run_errors and not force:
            report.plan.insert(
                0,
                f"NOTE: a real publish would refuse — the source run recorded "
                f"{run_errors} error-severity exception(s) (--force required)",
            )
        logger.info("dry run target=%s rows=%d — nothing written", name, frame.height)
        return report

    started_at = datetime.now(timezone.utc)
    try:
        outcome = target.publish(frame)
    except (TargetError, SecretError) as exc:
        finished_at = datetime.now(timezone.utc)
        report.outcome = "failed"
        report.duration_seconds = (finished_at - started_at).total_seconds()
        report.manifest_path = _append_manifest(root, report, started_at, finished_at)
        raise PublishError(f"publish to target '{name}' failed: {exc}") from exc
    finished_at = datetime.now(timezone.utc)

    report.rows_sent = outcome.rows_sent
    report.rows_failed = len(outcome.failures)
    report.duration_seconds = (finished_at - started_at).total_seconds()
    if report.rows_failed == 0:
        report.outcome = "published"
    elif report.rows_sent:
        report.outcome = "partial"
    else:
        report.outcome = "failed"

    if outcome.failures:
        exceptions_csv = root / config.output.directory / PUBLISH_EXCEPTIONS_NAME
        write_exceptions(_failure_records(report, target, outcome.failures), exceptions_csv)
        report.exceptions_csv = exceptions_csv

    report.manifest_path = _append_manifest(root, report, started_at, finished_at)
    logger.info(
        "publish complete target=%s outcome=%s sent=%d failed=%d",
        name,
        report.outcome,
        report.rows_sent,
        report.rows_failed,
    )
    return report


def _append_manifest(
    root: Path, report: PublishReport, started_at: datetime, finished_at: datetime
) -> Path:
    record = {
        "target": report.target,
        "type": report.target_type,
        "destination": redact_text(report.destination),
        "source_run": report.source_run,
        "rows": report.rows,
        "rows_sent": report.rows_sent,
        "rows_failed": report.rows_failed,
        "outcome": report.outcome,
        "forced": report.forced,
    }
    if report.forced:
        record["forced_note"] = (
            "published despite error-severity exceptions in the source run; "
            "--force was given"
        )
    path = write_publish_manifest(
        root / RUNS_DIRECTORY,
        started_at=started_at,
        finished_at=finished_at,
        publish=record,
    )
    # Nothing in a manifest may hold a secret, however a target misbehaves.
    text = path.read_text(encoding="utf-8")
    redacted = redact_text(text)
    if redacted != text:
        path.write_text(redacted, encoding="utf-8")
    return path
