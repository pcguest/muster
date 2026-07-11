"""Unit tests for exception capture and the exceptions.csv writer."""

import polars as pl

from muster.config import Config, FieldSpec
from muster.pipeline import discover_sources
from muster.records import ExceptionRecord, write_exceptions


def test_write_exceptions_produces_expected_columns(tmp_path):
    path = tmp_path / "exceptions.csv"
    write_exceptions(
        [
            ExceptionRecord(
                file="a.csv", row=4, column="Total Spend", value="n/a",
                reason="cannot coerce to float for field 'lifetime_value'",
            ),
            ExceptionRecord(file="a.csv", column="Notes", reason="unmapped column"),
        ],
        path,
    )
    frame = pl.read_csv(path)
    assert frame.columns == ["file", "row", "column", "value", "reason"]
    assert frame.height == 2
    assert frame.row(0) == (
        "a.csv", 4, "Total Spend", "n/a",
        "cannot coerce to float for field 'lifetime_value'",
    )


def test_write_exceptions_with_no_records_writes_header_only(tmp_path):
    path = tmp_path / "exceptions.csv"
    write_exceptions([], path)
    frame = pl.read_csv(path)
    assert frame.columns == ["file", "row", "column", "value", "reason"]
    assert frame.height == 0


def test_oversized_file_is_recorded_not_read(tmp_path):
    big = tmp_path / "big.csv"
    big.write_text("a,b\n" + "x,y\n" * 400_000, encoding="utf-8")
    config = Config(
        fields=[FieldSpec(name="a")],
        sources=["*.csv"],
        limits={"max_file_size_mb": 1},
    )
    files, exceptions = discover_sources(config, tmp_path)
    assert files == []
    assert len(exceptions) == 1
    assert exceptions[0].file == "big.csv"
    assert "over the 1 MB limit" in exceptions[0].reason


def test_hidden_directories_are_skipped(tmp_path):
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "stale.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "real.csv").write_text("a\n1\n", encoding="utf-8")
    config = Config(fields=[FieldSpec(name="a")], sources=["**/*.csv"])
    files, exceptions = discover_sources(config, tmp_path)
    assert [f.name for f in files] == ["real.csv"]
    assert exceptions == []
