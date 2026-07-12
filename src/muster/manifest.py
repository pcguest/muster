"""Run manifests: a tamper-evident audit trail of every run and publish.

Each pipeline run writes ``runs/<timestamp>/manifest.json`` recording the
SHA-256 of the configuration, of every input file and of every output, plus
row and exception counts and the run duration. Every publish appends its own
manifest to the same chain (``kind: publish``) recording the target, row
counts, duration and outcome — including refusals overridden with --force.
Every manifest embeds the SHA-256 of the previous manifest, forming a hash
chain: altering any historic manifest breaks verification of every manifest
after it. This is the integrity leg of the CIA triad — the lineage of the
governed dataset, and of everywhere it was sent, can be checked rather than
trusted.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from muster import __version__

logger = logging.getLogger(__name__)

RUNS_DIRECTORY = "runs"
MANIFEST_NAME = "manifest.json"


class ManifestError(RuntimeError):
    """Raised when the manifest chain fails verification."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_directories(runs_dir: Path) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    return sorted(
        entry
        for entry in runs_dir.iterdir()
        if entry.is_dir() and (entry / MANIFEST_NAME).is_file()
    )


def latest_run_directory(runs_dir: Path) -> Path | None:
    """The most recent run directory holding a manifest, if any."""
    directories = _run_directories(runs_dir)
    return directories[-1] if directories else None


def latest_manifest_of_kind(runs_dir: Path, kind: str) -> tuple[Path, dict] | None:
    """The newest manifest of one kind (``run`` or ``publish``), parsed.

    Publish manifests share the chain with pipeline-run manifests, so the
    newest directory is not necessarily the newest *run*. Manifests written
    before publishes existed carry no ``kind`` and count as runs.
    """
    for run_dir in reversed(_run_directories(runs_dir)):
        path = run_dir / MANIFEST_NAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("kind", "run") == kind:
            return path, manifest
    return None


def create_run_directory(runs_dir: Path, started_at: datetime) -> Path:
    """Create a fresh, uniquely named directory for one run's artefacts."""
    stamp = started_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = runs_dir / stamp
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = runs_dir / f"{stamp}-{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def write_manifest(
    run_dir: Path,
    *,
    started_at: datetime,
    finished_at: datetime,
    config_path: Path,
    inputs: Sequence[tuple[str, Path, int]],
    outputs: Mapping[str, Path],
    totals: Mapping[str, int],
) -> Path:
    """Write the manifest for one run, chained to the previous manifest.

    ``run_dir`` comes from :func:`create_run_directory`; its siblings that
    already hold a manifest are the run history. ``inputs`` is (name, path,
    rows) per source file; ``outputs`` maps output names to written files.
    Returns the manifest path.
    """
    manifest = {
        "run_id": run_dir.name,
        "kind": "run",
        "muster_version": __version__,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": finished_at.astimezone(timezone.utc).isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "config": {"file": config_path.name, "sha256": sha256_file(config_path)},
        "inputs": [
            {
                "file": name,
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
                "rows": rows,
            }
            for name, path, rows in inputs
        ],
        "outputs": {
            name: {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}
            for name, path in outputs.items()
        },
        "totals": dict(totals),
    }
    return _write_chained(run_dir, manifest)


def write_publish_manifest(
    runs_dir: Path,
    *,
    started_at: datetime,
    finished_at: datetime,
    publish: Mapping[str, object],
) -> Path:
    """Append one publish to the manifest chain.

    ``publish`` records the target, destination, source run, row counts,
    outcome and whether error-severity exceptions were overridden with
    --force. Failed publishes are recorded too — the audit trail covers what
    was attempted, not just what succeeded.
    """
    run_dir = create_run_directory(runs_dir, started_at)
    manifest = {
        "run_id": run_dir.name,
        "kind": "publish",
        "muster_version": __version__,
        "started_at": started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": finished_at.astimezone(timezone.utc).isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "publish": dict(publish),
    }
    return _write_chained(run_dir, manifest)


def _write_chained(run_dir: Path, manifest: dict) -> Path:
    """Write a manifest with the link to its predecessor, completing the chain."""
    previous = None
    latest = latest_run_directory(run_dir.parent)
    if latest is not None and latest != run_dir:
        previous = {
            "run_id": latest.name,
            "sha256": sha256_file(latest / MANIFEST_NAME),
        }
    manifest["previous_manifest"] = previous
    path = run_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote manifest path=%s previous=%s", path, previous and previous["run_id"])
    return path


def verify_chain(runs_dir: Path) -> list[str]:
    """Verify every manifest links to its predecessor; return run ids in order.

    Raises :class:`ManifestError` naming the first run whose link is broken —
    a missing, reordered or altered historic manifest.
    """
    run_ids: list[str] = []
    previous_path: Path | None = None
    for run_dir in _run_directories(runs_dir):
        path = run_dir / MANIFEST_NAME
        manifest = json.loads(path.read_text(encoding="utf-8"))
        link = manifest.get("previous_manifest")
        if previous_path is None:
            if link is not None:
                raise ManifestError(
                    f"run {run_dir.name} names a predecessor but none exists on disk"
                )
        else:
            expected = sha256_file(previous_path)
            if not link or link.get("sha256") != expected:
                raise ManifestError(
                    f"manifest chain broken at run {run_dir.name}: "
                    f"predecessor {previous_path.parent.name} does not match its recorded hash"
                )
        run_ids.append(run_dir.name)
        previous_path = path
    return run_ids
