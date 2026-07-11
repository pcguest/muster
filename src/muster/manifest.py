"""Run manifests: a tamper-evident audit trail of every pipeline run.

Each run writes ``runs/<timestamp>/manifest.json`` recording the SHA-256 of
the configuration, of every input file and of every output, plus row and
exception counts and the run duration. Every manifest also embeds the SHA-256
of the previous run's manifest, forming a hash chain: altering any historic
manifest breaks verification of every manifest after it. This is the
integrity leg of the CIA triad — the lineage of the governed dataset can be
checked, not just trusted.
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
    previous = None
    latest = latest_run_directory(run_dir.parent)
    if latest is not None:
        previous = {
            "run_id": latest.name,
            "sha256": sha256_file(latest / MANIFEST_NAME),
        }

    manifest = {
        "run_id": run_dir.name,
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
        "previous_manifest": previous,
    }
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
