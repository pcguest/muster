"""Unit tests for the tamper-evident manifest chain."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from muster.manifest import (
    ManifestError,
    create_run_directory,
    latest_run_directory,
    sha256_file,
    verify_chain,
    write_manifest,
)


def _write_run(tmp_path: Path, started: datetime, rows: int) -> Path:
    config = tmp_path / "muster.yaml"
    config.write_text("fields: []\n", encoding="utf-8")
    source = tmp_path / "input.csv"
    source.write_text(f"a\n{rows}\n", encoding="utf-8")
    output = tmp_path / "out.csv"
    output.write_text("a\n", encoding="utf-8")
    return write_manifest(
        create_run_directory(tmp_path / "runs", started),
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        config_path=config,
        inputs=[("input.csv", source, rows)],
        outputs={"consolidated.csv": output},
        totals={"rows_in": rows, "rows_published": rows, "rows_held": 0},
    )


def test_manifest_records_hashes_and_totals(tmp_path):
    started = datetime(2026, 7, 11, 9, 0, 0, tzinfo=timezone.utc)
    path = _write_run(tmp_path, started, rows=4)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == "20260711T090000Z"
    assert manifest["previous_manifest"] is None
    assert manifest["config"]["sha256"] == sha256_file(tmp_path / "muster.yaml")
    assert manifest["inputs"][0]["sha256"] == sha256_file(tmp_path / "input.csv")
    assert manifest["totals"]["rows_in"] == 4
    assert manifest["duration_seconds"] == 1.0


def test_chain_links_and_verifies(tmp_path):
    started = datetime(2026, 7, 11, 9, 0, 0, tzinfo=timezone.utc)
    first = _write_run(tmp_path, started, rows=4)
    second = _write_run(tmp_path, started + timedelta(minutes=5), rows=5)
    manifest = json.loads(second.read_text(encoding="utf-8"))
    assert manifest["previous_manifest"] == {
        "run_id": "20260711T090000Z",
        "sha256": sha256_file(first),
    }
    assert verify_chain(tmp_path / "runs") == ["20260711T090000Z", "20260711T090500Z"]


def test_tampered_manifest_breaks_the_chain(tmp_path):
    started = datetime(2026, 7, 11, 9, 0, 0, tzinfo=timezone.utc)
    first = _write_run(tmp_path, started, rows=4)
    _write_run(tmp_path, started + timedelta(minutes=5), rows=5)
    doctored = json.loads(first.read_text(encoding="utf-8"))
    doctored["totals"]["rows_in"] = 400  # quietly rewrite history
    first.write_text(json.dumps(doctored, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="chain broken at run 20260711T090500Z"):
        verify_chain(tmp_path / "runs")


def test_same_second_runs_get_distinct_directories(tmp_path):
    started = datetime(2026, 7, 11, 9, 0, 0, tzinfo=timezone.utc)
    _write_run(tmp_path, started, rows=1)
    second = _write_run(tmp_path, started, rows=2)
    assert second.parent.name == "20260711T090000Z-2"
    assert latest_run_directory(tmp_path / "runs").name == "20260711T090000Z-2"
    assert len(verify_chain(tmp_path / "runs")) == 2
