"""End-to-end test: init, profile and run over three disagreeing fixtures.

The fixtures share one customer schema but disagree on column names, date
formats, number styles and boolean spellings, and include one unmappable
column and two uncoercible cells.
"""

import json
import shutil
from pathlib import Path

import polars as pl
from typer.testing import CliRunner

from muster.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_FILES = ["customers_north.csv", "clients_south.csv", "customers_west.xlsx"]

runner = CliRunner()


def test_init_profile_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    for name in FIXTURE_FILES:
        shutil.copy(FIXTURES / name, sources / name)

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "muster.yaml").is_file()

    result = runner.invoke(app, ["profile", "sources"])
    assert result.exit_code == 0, result.output
    report = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    assert len(report["files"]) == 3
    south = next(f for f in report["files"] if f["file"] == "clients_south.csv")
    assert south["rows"] == 4
    spend = next(c for c in south["columns"] if c["name"] == "Total Spend")
    assert any("thousands separators" in issue for issue in spend["issues"])

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output

    consolidated = pl.read_parquet(tmp_path / "output" / "consolidated.parquet")
    assert consolidated.height == 12
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
    # Three date formats normalised to one representation.
    dates = dict(
        zip(
            consolidated.get_column("customer_id").to_list(),
            consolidated.get_column("signup_date").to_list(),
        )
    )
    assert str(dates["C-001"]) == "2023-04-12"  # ISO in the north file
    assert str(dates["C-010"]) == "2023-02-14"  # day-first in the south file
    assert str(dates["C-020"]) == "2024-03-05"  # '05 Mar 2024' in the west file
    assert dates["C-022"] is None  # uncoercible, captured as an exception
    # Thousands separators normalised; failed cell nulled, not guessed.
    values = dict(
        zip(
            consolidated.get_column("customer_id").to_list(),
            consolidated.get_column("lifetime_value").to_list(),
        )
    )
    assert values["C-010"] == 1204.5
    assert values["C-012"] is None

    csv_copy = pl.read_csv(tmp_path / "output" / "consolidated.csv")
    assert csv_copy.height == 12

    exceptions = pl.read_csv(tmp_path / "output" / "exceptions.csv")
    assert exceptions.height == 3
    rows = {
        (r["file"], r["column"]): r for r in exceptions.iter_rows(named=True)
    }
    unmapped = rows[("sources/clients_south.csv", "Notes")]
    assert "unmapped column" in unmapped["reason"]
    bad_float = rows[("sources/clients_south.csv", "Total Spend")]
    assert bad_float["row"] == 4
    assert bad_float["value"] == "n/a"
    assert "cannot coerce to float" in bad_float["reason"]
    bad_date = rows[("sources/customers_west.xlsx", "Signup Date")]
    assert bad_date["row"] == 4
    assert bad_date["value"] == "sometime in June"
    assert "cannot coerce to date" in bad_date["reason"]


def test_run_is_repeatable_without_consuming_its_own_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    shutil.copy(FIXTURES / "customers_north.csv", sources / "customers_north.csv")
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["run"]).exit_code == 0
    assert runner.invoke(app, ["run"]).exit_code == 0
    consolidated = pl.read_parquet(tmp_path / "output" / "consolidated.parquet")
    assert consolidated.height == 4
