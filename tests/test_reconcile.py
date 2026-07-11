"""Unit tests for duplicate detection and survivorship strategies."""

import polars as pl

from muster.config import Config
from muster.reconcile import reconcile


def _config(survivorship=None):
    validation = {"keys": ["id"]}
    if survivorship is not None:
        validation["survivorship"] = survivorship
    return Config.model_validate(
        {
            "fields": [
                {"name": "id", "type": "string", "required": True},
                {"name": "amount", "type": "float"},
                {"name": "city", "type": "string"},
            ],
            "validation": validation,
        }
    )


def _frame(rows):
    return pl.DataFrame(
        rows,
        schema={
            "_source_file": pl.String,
            "_row": pl.Int64,
            "id": pl.String,
            "amount": pl.Float64,
            "city": pl.String,
        },
        orient="row",
    )


def test_unique_keys_pass_through_untouched():
    frame = _frame([("a.csv", 2, "K1", 10.0, "Perth"), ("a.csv", 3, "K2", 11.0, None)])
    result = reconcile(frame, _config())
    assert result.frame.height == 2
    assert result.exceptions == []
    assert result.rows_held == 0


def test_agreeing_duplicates_merge_with_warning():
    frame = _frame(
        [
            ("a.csv", 2, "K1", 10.0, None),
            ("b.csv", 5, "K1", 10.0, "Perth"),  # agrees; adds city
        ]
    )
    result = reconcile(frame, _config())
    assert result.frame.height == 1
    merged = result.frame.row(0, named=True)
    assert merged["amount"] == 10.0
    assert merged["city"] == "Perth"
    assert merged["_source_file"] == "a.csv; b.csv"
    assert result.rows_superseded == 1
    assert [r.kind for r in result.exceptions] == ["duplicate_key"]
    assert result.exceptions[0].severity == "warning"


def test_conflict_without_strategy_holds_all_rows():
    frame = _frame(
        [
            ("a.csv", 2, "K1", 10.0, "Perth"),
            ("b.csv", 5, "K1", 12.0, "Perth"),
        ]
    )
    result = reconcile(frame, _config())
    assert result.frame.height == 0
    assert result.rows_held == 2
    assert result.conflicts_held == 1
    (record,) = result.exceptions
    assert (record.kind, record.severity, record.column) == ("conflict", "error", "amount")
    assert record.value == "a.csv row 2: 10.0 | b.csv row 5: 12.0"
    assert "no survivorship strategy configured" in record.reason


def test_manual_strategy_holds_conflicts():
    frame = _frame(
        [("a.csv", 2, "K1", 10.0, None), ("b.csv", 5, "K1", 12.0, None)]
    )
    result = reconcile(frame, _config({"strategy": "manual"}))
    assert result.frame.height == 0
    assert result.rows_held == 2
    assert "manual" in result.exceptions[0].reason


def test_newest_file_keeps_latest_source():
    frame = _frame(
        [("old.csv", 2, "K1", 10.0, None), ("new.csv", 5, "K1", 12.0, None)]
    )
    result = reconcile(
        frame,
        _config({"strategy": "newest_file"}),
        mtimes={"old.csv": 100.0, "new.csv": 200.0},
    )
    assert result.frame.height == 1
    assert result.frame.row(0, named=True)["amount"] == 12.0
    (record,) = result.exceptions
    assert record.severity == "warning"
    assert "resolved by newest_file: kept new.csv" in record.reason


def test_newest_file_holds_on_equal_timestamps():
    frame = _frame(
        [("a.csv", 2, "K1", 10.0, None), ("b.csv", 5, "K1", 12.0, None)]
    )
    result = reconcile(
        frame,
        _config({"strategy": "newest_file"}),
        mtimes={"a.csv": 100.0, "b.csv": 100.0},
    )
    assert result.frame.height == 0
    assert result.rows_held == 2
    assert "cannot decide" in result.exceptions[0].reason


def test_priority_list_keeps_earliest_listed_file():
    frame = _frame(
        [("regional.csv", 2, "K1", 10.0, None), ("master.csv", 5, "K1", 12.0, None)]
    )
    result = reconcile(
        frame,
        _config({"strategy": "priority_list", "priority": ["master.csv", "regional.csv"]}),
    )
    assert result.frame.height == 1
    assert result.frame.row(0, named=True)["amount"] == 12.0
    assert "resolved by priority_list: kept master.csv" in result.exceptions[0].reason


def test_priority_list_holds_unlisted_conflicts():
    frame = _frame(
        [("a.csv", 2, "K1", 10.0, None), ("b.csv", 5, "K1", 12.0, None)]
    )
    result = reconcile(
        frame, _config({"strategy": "priority_list", "priority": ["master.csv"]})
    )
    assert result.frame.height == 0
    assert "no conflicting source appears in the priority list" in result.exceptions[0].reason


def test_empty_key_rows_are_held():
    frame = _frame([("a.csv", 2, None, 10.0, None), ("a.csv", 3, "K2", 11.0, None)])
    result = reconcile(frame, _config())
    assert result.frame.height == 1
    assert result.rows_held == 1
    (record,) = result.exceptions
    assert (record.kind, record.row) == ("missing_key", 2)
