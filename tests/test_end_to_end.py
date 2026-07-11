"""End-to-end test: init, profile, run and report over disagreeing fixtures.

The fixtures share one customer schema but disagree on column names, date
formats, number styles and boolean spellings, and deliberately include an
unmappable column, uncoercible cells, rule violations (bad email, negative
lifetime value), an agreeing duplicate key and a conflicting duplicate key.
"""

import json
import shutil
from pathlib import Path

import polars as pl
from typer.testing import CliRunner

from muster.cli import app
from muster.manifest import verify_chain

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_FILES = [
    "customers_north.csv",
    "clients_south.csv",
    "customers_west.xlsx",
    "customers_east.csv",
]

runner = CliRunner()


def _stage(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    for name in FIXTURE_FILES:
        shutil.copy(FIXTURES / name, sources / name)


def test_init_profile_run_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _stage(tmp_path)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "muster.yaml").is_file()

    result = runner.invoke(app, ["profile", "sources"])
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    assert len(report["files"]) == 4
    south = next(f for f in report["files"] if f["file"] == "clients_south.csv")
    assert south["rows"] == 4
    spend = next(c for c in south["columns"] if c["name"] == "Total Spend")
    assert any("thousands separators" in issue for issue in spend["issues"])

    # Error-severity exceptions make the run exit 2 — CI/cron friendly.
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2, result.output
    assert "Published 11 of 16 row(s) from 4 file(s); 4 held, 1 superseded." in result.output
    assert "report:" in result.output
    assert "manifest:" in result.output

    consolidated = pl.read_parquet(tmp_path / "output" / "consolidated.parquet")
    assert consolidated.height == 11
    assert consolidated.columns == [
        "_source_file",
        "customer_id",
        "full_name",
        "email",
        "signup_date",
        "lifetime_value",
        "active",
    ]
    assert consolidated.get_column("signup_date").dtype == pl.Date
    rows = {
        r["customer_id"]: r for r in consolidated.iter_rows(named=True)
    }

    # Three date formats normalised to one representation.
    assert str(rows["C-002"]["signup_date"]) == "2022-11-30"  # ISO in the north file
    assert str(rows["C-010"]["signup_date"]) == "2023-02-14"  # day-first in the south file
    assert str(rows["C-020"]["signup_date"]) == "2024-03-05"  # '05 Mar 2024' in the west file
    # Thousands separators normalised.
    assert rows["C-010"]["lifetime_value"] == 1204.5

    # Error-severity rows are held out of the governed dataset:
    # C-001 conflicts across files, C-012 and C-022 failed coercion.
    for held in ("C-001", "C-012", "C-022"):
        assert held not in rows
    # Agreeing duplicate merged: north's email survives (east's is empty)
    # and provenance names both files, in discovery order.
    merged = rows["C-004"]
    assert merged["email"] == "dev.sharma@example.com"
    assert merged["_source_file"] == (
        "sources/customers_east.csv; sources/customers_north.csv"
    )
    # Warning-severity violations are published and reported.
    assert rows["C-030"]["email"] == "not-an-email"
    assert rows["C-031"]["lifetime_value"] == -20.0

    exceptions = pl.read_csv(tmp_path / "output" / "exceptions.csv")
    assert exceptions.columns == ["file", "row", "column", "value", "kind", "severity", "reason"]
    by_kind = {
        (r["kind"], r["severity"]): r for r in exceptions.iter_rows(named=True)
    }
    assert exceptions.height == 7
    assert ("unmapped_column", "warning") in by_kind
    assert ("rule_regex", "warning") in by_kind
    assert by_kind[("rule_regex", "warning")]["value"] == "not-an-email"
    assert ("rule_range", "warning") in by_kind
    assert by_kind[("rule_range", "warning")]["value"] == "-20.0"
    assert ("duplicate_key", "warning") in by_kind
    conflict = by_kind[("conflict", "error")]
    assert conflict["column"] == "lifetime_value"
    assert "1520.75" in conflict["value"] and "1600.0" in conflict["value"]
    assert "no survivorship strategy configured" in conflict["reason"]
    coercions = exceptions.filter(pl.col("kind") == "coercion")
    assert coercions.height == 2
    assert set(coercions.get_column("value").to_list()) == {"n/a", "sometime in June"}
    assert (coercions.get_column("severity") == "error").all()

    # The report and the chained manifest exist and carry the run's numbers.
    report_html = (tmp_path / "output" / "report.html").read_text(encoding="utf-8")
    for expected in ("Muster run report", "rows published", "Conflicts held for review"):
        assert expected in report_html
    run_ids = verify_chain(tmp_path / "runs")
    assert len(run_ids) == 1
    manifest = json.loads(
        (tmp_path / "runs" / run_ids[0] / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["totals"] == {
        "rows_in": 16,
        "rows_published": 11,
        "rows_held": 4,
        "rows_superseded": 1,
        "errors": 3,
        "warnings": 4,
    }
    assert len(manifest["inputs"]) == 4
    assert manifest["previous_manifest"] is None

    # muster report re-renders the archived run without touching the sources.
    (tmp_path / "output" / "report.html").unlink()
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert "Muster run report" in (tmp_path / "output" / "report.html").read_text(
        encoding="utf-8"
    )


def test_reruns_chain_manifests_and_ignore_own_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    shutil.copy(FIXTURES / "customers_north.csv", sources / "customers_north.csv")
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["run"]).exit_code == 0
    assert runner.invoke(app, ["run"]).exit_code == 0
    consolidated = pl.read_parquet(tmp_path / "output" / "consolidated.parquet")
    assert consolidated.height == 4
    assert len(verify_chain(tmp_path / "runs")) == 2
