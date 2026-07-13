"""Remediation: corrected rows rejoin the governed dataset under audit.

Every test drives the real pipeline over a small project: a held row is
corrected through the same append-only audit log the dashboard and CLI
write, and the next run applies, re-validates and recovers it — or refuses
with a written exception when the correction is not good enough.
"""

import json

import polars as pl
import pytest
from typer.testing import CliRunner

from muster.cli import app
from muster.config import load_config
from muster.pipeline import run_pipeline
from muster.remediation import (
    append_resolution,
    check_correction,
    load_corrections,
    load_exceptions,
    resolutions_path,
)

runner = CliRunner()

CONFIG = """\
fields:
  - name: customer_id
    type: string
    required: true
  - name: spend
    type: float
    required: true
    rules:
      - rule: range
        min: 0
        max: 100
        severity: error
  - name: visits
    type: integer
  - name: category
    type: string
    rules:
      - rule: allowed_values
        values: ["retail", "trade"]
        severity: error
sources: ["*.csv"]
validation:
  keys: ["customer_id"]
"""

# The header is row 1. C-1 (row 2) is clean; C-2 (row 3) fails coercion on
# spend; C-3 (row 4) fails the range rule; C-4 (row 5) fails coercion on
# both spend and visits — two errors on one row.
CSV = """\
customer_id,spend,visits,category
C-1,10.5,3,retail
C-2,zzz,1,retail
C-3,500,2,trade
C-4,zzz,zzz,trade
"""


@pytest.fixture()
def project(tmp_path):
    (tmp_path / "muster.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "data.csv").write_text(CSV, encoding="utf-8")
    return tmp_path


def _run(project):
    config = load_config(project / "muster.yaml")
    return config, run_pipeline(config, project, project / "muster.yaml")


def _exception(project, config, **wanted):
    matches = [
        row
        for row in load_exceptions(project, config)
        if all(getattr(row, key) == value for key, value in wanted.items())
    ]
    assert len(matches) == 1, f"expected exactly one exception matching {wanted}"
    return matches[0]


def test_correction_recovers_a_held_row_end_to_end(project):
    config, first = _run(project)
    assert first.rows_published == 1
    assert first.rows_remediated == 0

    held = _exception(project, config, kind="coercion", file="data.csv", row="3")
    append_resolution(project, held.id, "corrected", "till receipt says 20.5", {"spend": "20.5"})

    config, second = _run(project)
    assert second.rows_published == 2
    assert second.rows_remediated == 1
    assert second.remediation_resolutions == [held.id]

    governed = pl.read_parquet(second.output_parquet)
    recovered = governed.filter(pl.col("customer_id") == "C-2")
    assert recovered.get_column("spend")[0] == 20.5

    # The original error record is replaced by one warning telling the story;
    # the manifest names the decision that put the row in.
    exceptions = pl.read_csv(second.exceptions_csv, infer_schema=False)
    remediated = exceptions.filter(pl.col("kind") == "remediated")
    assert remediated.height == 1
    assert remediated.get_column("severity")[0] == "warning"
    assert held.id in remediated.get_column("reason")[0]
    assert exceptions.filter(
        (pl.col("kind") == "coercion") & (pl.col("row") == "3")
    ).is_empty()
    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert manifest["totals"]["rows_remediated"] == 1
    assert manifest["remediation"]["resolutions"] == [held.id]

    # The correction is a standing decision: a third run over the unchanged
    # sources applies it again, deterministically.
    _, third = _run(project)
    assert third.rows_published == 2
    assert third.rows_remediated == 1


def test_a_correction_that_still_fails_rules_stays_held(project):
    config, _ = _run(project)
    held = _exception(project, config, kind="rule_range", file="data.csv", row="4")
    # Recorded directly into the audit log, bypassing the form/CLI checks —
    # the pipeline must still refuse it on re-validation.
    append_resolution(project, held.id, "corrected", "typo", {"spend": "999"})

    _, result = _run(project)
    assert result.rows_remediated == 0
    assert result.rows_published == 1
    exceptions = pl.read_csv(result.exceptions_csv, infer_schema=False)
    failed = exceptions.filter(pl.col("kind") == "remediation_failed")
    assert failed.height == 1
    reason = failed.get_column("reason")[0]
    assert held.id in reason and "still fail" in reason
    assert failed.get_column("severity")[0] == "error"
    # The original defect record is kept: the row is held, not forgotten.
    assert not exceptions.filter(
        (pl.col("kind") == "rule_range") & (pl.col("severity") == "error")
    ).is_empty()


def test_a_partly_corrected_row_stays_held(project):
    config, _ = _run(project)
    # C-4 carries two coercion errors; correct only the spend one.
    held = _exception(project, config, kind="coercion", row="5", column="spend")
    append_resolution(project, held.id, "corrected", "receipt", {"spend": "12.0"})

    _, result = _run(project)
    assert result.rows_remediated == 0
    exceptions = pl.read_csv(result.exceptions_csv, infer_schema=False)
    failed = exceptions.filter(pl.col("kind") == "remediation_failed")
    assert failed.height == 1
    assert "only some" in failed.get_column("reason")[0]


def test_newer_records_supersede_older_ones(project):
    config, _ = _run(project)
    held = _exception(project, config, kind="coercion", row="3")

    append_resolution(project, held.id, "corrected", "first guess", {"spend": "999"})
    append_resolution(project, held.id, "corrected", "checked the receipt", {"spend": "20.5"})
    assert load_corrections(project)[held.id].values == {"spend": "20.5"}

    _, result = _run(project)
    assert result.rows_remediated == 1
    governed = pl.read_parquet(result.output_parquet)
    assert governed.filter(pl.col("customer_id") == "C-2").get_column("spend")[0] == 20.5

    # A later dismissal withdraws the correction entirely.
    append_resolution(project, held.id, "dismissed", "source will be fixed instead")
    assert held.id not in load_corrections(project)
    _, result = _run(project)
    assert result.rows_remediated == 0
    # History was never rewritten: all four decisions are still on file.
    lines = resolutions_path(project).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_fingerprints_are_stable_across_runs(project):
    config, _ = _run(project)
    first = {(row.id, row.kind) for row in load_exceptions(project, config)}
    _run(project)
    second = {(row.id, row.kind) for row in load_exceptions(project, config)}
    assert first == second
    assert first  # the project is deliberately dirty


def test_check_correction_reports_precise_failures(project):
    config = load_config(project / "muster.yaml")
    assert check_correction(config, {}) == ["no corrected values given"]
    assert check_correction(config, {"nope": "1"}) == ["unknown field 'nope'"]
    assert check_correction(config, {"spend": "zzz"}) == [
        "'zzz' cannot be coerced to float for field 'spend'"
    ]
    range_failures = check_correction(config, {"spend": "500"})
    assert len(range_failures) == 1
    assert "out of range for field 'spend'" in range_failures[0]
    assert check_correction(config, {"category": "wholesale"}) == [
        "value not in allowed values for field 'category'"
    ]
    assert check_correction(config, {"spend": "20.5", "category": "trade"}) == []


def test_cli_resolve_records_and_rejects(project, monkeypatch):
    monkeypatch.chdir(project)
    assert runner.invoke(app, ["run"]).exit_code == 2
    config = load_config(project / "muster.yaml")
    held = _exception(project, config, kind="coercion", row="3")

    rejected = runner.invoke(
        app, ["resolve", held.id, "--set", "spend=zzz", "--note", "oops"]
    )
    assert rejected.exit_code == 1
    assert "cannot be coerced" in rejected.output

    missing = runner.invoke(
        app, ["resolve", "0" * 16, "--set", "spend=1", "--note", "x"]
    )
    assert missing.exit_code == 1
    assert "no exception with fingerprint" in missing.output

    malformed = runner.invoke(
        app, ["resolve", held.id, "--set", "spend", "--note", "x"]
    )
    assert malformed.exit_code == 1
    assert "field=value" in malformed.output

    accepted = runner.invoke(
        app,
        ["resolve", held.id, "--set", "spend=20.5", "--note", "till receipt says 20.5"],
    )
    assert accepted.exit_code == 0, accepted.output
    assert "Correction recorded" in accepted.output

    rerun = runner.invoke(app, ["run"])
    assert rerun.exit_code == 2  # C-3 and C-4 are still deliberately broken
    assert "1 row(s) recovered via remediation" in " ".join(rerun.output.split())
