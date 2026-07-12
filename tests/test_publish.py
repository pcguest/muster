"""'muster publish': sqlite end to end, refusal, dry-run, integrity, audit."""

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from muster.cli import app
from muster.config import Config
from muster.manifest import verify_chain
from muster.publish import PublishError, select_target

runner = CliRunner()

CONFIG = """\
fields:
  - name: customer_id
    type: string
    required: true
  - name: spend
    type: float
sources: ["*.csv"]
validation:
  keys: ["customer_id"]
targets:
  warehouse:
    type: sqlite
    path: warehouse.db
    table: customers
"""

GOOD_CSV = "customer_id,spend\nC-1,10.5\nC-2,20.0\n"
BAD_CSV = "customer_id,spend\nC-1,10.5\nC-2,not-a-number\n"


def _project(tmp_path, csv_text=GOOD_CSV, config=CONFIG):
    (tmp_path / "muster.yaml").write_text(config, encoding="utf-8")
    (tmp_path / "data.csv").write_text(csv_text, encoding="utf-8")


def _rows(db: Path) -> list[tuple]:
    connection = sqlite3.connect(db)
    try:
        return connection.execute(
            "SELECT customer_id, spend FROM customers ORDER BY customer_id"
        ).fetchall()
    finally:
        connection.close()


def test_sqlite_publish_end_to_end_upserts_idempotently(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    assert runner.invoke(app, ["run"]).exit_code == 0

    # No target name needed when exactly one is configured.
    first = runner.invoke(app, ["publish"])
    assert first.exit_code == 0, first.output
    assert "Published 2 of 2 row(s)" in first.output
    assert _rows(tmp_path / "warehouse.db") == [("C-1", 10.5), ("C-2", 20.0)]

    # Republishing the same dataset changes nothing: upsert on the key.
    assert runner.invoke(app, ["publish", "warehouse"]).exit_code == 0
    assert _rows(tmp_path / "warehouse.db") == [("C-1", 10.5), ("C-2", 20.0)]

    # A changed value flows through as an update, not a duplicate.
    (tmp_path / "data.csv").write_text(
        "customer_id,spend\nC-1,10.5\nC-2,30.0\n", encoding="utf-8"
    )
    assert runner.invoke(app, ["run"]).exit_code == 0
    assert runner.invoke(app, ["publish", "warehouse"]).exit_code == 0
    assert _rows(tmp_path / "warehouse.db") == [("C-1", 10.5), ("C-2", 30.0)]


def test_publish_refuses_an_error_run_until_forced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path, csv_text=BAD_CSV)
    assert runner.invoke(app, ["run"]).exit_code == 2  # the coercion error

    refused = runner.invoke(app, ["publish", "warehouse"])
    assert refused.exit_code == 1
    assert "refusing to publish" in refused.output
    assert not (tmp_path / "warehouse.db").exists()

    forced = runner.invoke(app, ["publish", "warehouse", "--force"])
    assert forced.exit_code == 0, forced.output
    assert "FORCED" in forced.output
    assert (tmp_path / "warehouse.db").exists()

    # The override is recorded loudly in the manifest chain.
    run_ids = verify_chain(tmp_path / "runs")
    last = json.loads(
        (tmp_path / "runs" / run_ids[-1] / "manifest.json").read_text(encoding="utf-8")
    )
    assert last["kind"] == "publish"
    assert last["publish"]["forced"] is True
    assert "error-severity" in last["publish"]["forced_note"]
    assert last["publish"]["outcome"] == "published"


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    assert runner.invoke(app, ["run"]).exit_code == 0
    manifests_before = sorted(p.name for p in (tmp_path / "runs").iterdir())

    result = runner.invoke(app, ["publish", "warehouse", "--dry-run"])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "nothing will be written" in flat
    assert "upsert 2 row(s) on key (customer_id)" in flat

    assert not (tmp_path / "warehouse.db").exists()
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == manifests_before


def test_dry_run_on_an_error_run_notes_the_refusal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path, csv_text=BAD_CSV)
    assert runner.invoke(app, ["run"]).exit_code == 2
    result = runner.invoke(app, ["publish", "warehouse", "--dry-run"])
    assert result.exit_code == 0
    assert "a real publish would refuse" in " ".join(result.output.split())
    assert not (tmp_path / "warehouse.db").exists()


def test_a_tampered_dataset_is_refused_even_with_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    assert runner.invoke(app, ["run"]).exit_code == 0
    with (tmp_path / "output" / "consolidated.parquet").open("ab") as handle:
        handle.write(b"tampered")

    result = runner.invoke(app, ["publish", "warehouse", "--force"])
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "stale or has been altered" in flat
    assert not (tmp_path / "warehouse.db").exists()


def test_chain_stays_verifiable_and_report_still_finds_the_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    assert runner.invoke(app, ["run"]).exit_code == 0
    assert runner.invoke(app, ["publish", "warehouse"]).exit_code == 0
    assert runner.invoke(app, ["publish", "warehouse"]).exit_code == 0

    run_ids = verify_chain(tmp_path / "runs")
    kinds = [
        json.loads(
            (tmp_path / "runs" / run_id / "manifest.json").read_text(encoding="utf-8")
        ).get("kind")
        for run_id in run_ids
    ]
    assert kinds == ["run", "publish", "publish"]

    # 'muster report' must keep finding the latest *pipeline* run.
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert run_ids[0] in result.output


def test_target_selection_failures_name_the_options():
    fields = [{"name": "customer_id"}]
    no_targets = Config.model_validate({"fields": fields})
    with pytest.raises(PublishError, match="no publish targets configured"):
        select_target(no_targets, None)

    two = Config.model_validate(
        {
            "fields": fields,
            "targets": {
                "a": {"type": "sqlite", "table": "t"},
                "b": {"type": "sqlite", "table": "t"},
            },
        }
    )
    with pytest.raises(PublishError, match="name one of: a, b"):
        select_target(two, None)
    with pytest.raises(PublishError, match="configured targets: a, b"):
        select_target(two, "missing")


def test_publish_without_a_run_says_run_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _project(tmp_path)
    result = runner.invoke(app, ["publish", "warehouse"])
    assert result.exit_code == 1
    assert "run 'muster run' first" in result.output
