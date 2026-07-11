"""Unit tests for the row-level validation engine."""

from datetime import date

import polars as pl

from muster.config import Config
from muster.rules import held_row_set, validate_frame


def _frame(**columns):
    height = len(next(iter(columns.values())))
    base = {
        "_source_file": ["orders.csv"] * height,
        "_row": list(range(2, 2 + height)),
    }
    return pl.DataFrame(base | columns)


def test_required_rule_flags_empty_values_as_errors():
    config = Config.model_validate(
        {"fields": [{"name": "id", "type": "string", "required": True}]}
    )
    frame = _frame(id=["A", None, "C"])
    records = validate_frame(frame, config)
    assert len(records) == 1
    record = records[0]
    assert (record.kind, record.severity, record.row) == ("rule_required", "error", 3)
    assert "required field 'id' is empty" in record.reason


def test_numeric_range_rule_with_severity():
    config = Config.model_validate(
        {
            "fields": [
                {
                    "name": "amount",
                    "type": "float",
                    "rules": [{"rule": "range", "min": 0, "max": 100, "severity": "warning"}],
                }
            ]
        }
    )
    frame = _frame(amount=[50.0, -1.0, 101.0, None])
    records = validate_frame(frame, config)
    assert [(r.row, r.value, r.severity) for r in records] == [
        (3, "-1.0", "warning"),
        (4, "101.0", "warning"),
    ]
    assert all(r.kind == "rule_range" for r in records)


def test_date_range_rule_uses_iso_bounds():
    config = Config.model_validate(
        {
            "fields": [
                {
                    "name": "signup",
                    "type": "date",
                    "rules": [{"rule": "range", "min": "2020-01-01"}],
                }
            ]
        }
    )
    frame = _frame(signup=[date(2019, 12, 31), date(2020, 1, 1)])
    records = validate_frame(frame, config)
    assert [r.row for r in records] == [2]
    assert "min 2020-01-01" in records[0].reason


def test_regex_rule_matches_whole_value():
    config = Config.model_validate(
        {
            "fields": [
                {
                    "name": "code",
                    "type": "string",
                    "rules": [{"rule": "regex", "pattern": r"C-\d{3}"}],
                }
            ]
        }
    )
    # 'XC-123X' contains the pattern but does not match it in full.
    frame = _frame(code=["C-123", "XC-123X", None])
    records = validate_frame(frame, config)
    assert [(r.row, r.value, r.kind) for r in records] == [(3, "XC-123X", "rule_regex")]


def test_allowed_values_rule():
    config = Config.model_validate(
        {
            "fields": [
                {
                    "name": "region",
                    "type": "string",
                    "rules": [{"rule": "allowed_values", "values": ["north", "south"]}],
                }
            ]
        }
    )
    frame = _frame(region=["north", "east", None])
    records = validate_frame(frame, config)
    assert [(r.row, r.value) for r in records] == [(3, "east")]
    assert records[0].kind == "rule_allowed_values"


def test_cross_field_rule_ignores_empty_sides():
    config = Config.model_validate(
        {
            "fields": [
                {"name": "contract_date", "type": "date"},
                {"name": "delivered_date", "type": "date"},
            ],
            "validation": {
                "cross_field": [
                    {"field": "delivered_date", "operator": ">=", "other": "contract_date"}
                ]
            },
        }
    )
    frame = _frame(
        contract_date=[date(2024, 1, 10), date(2024, 1, 10), None],
        delivered_date=[date(2024, 1, 9), date(2024, 1, 20), date(2024, 1, 1)],
    )
    records = validate_frame(frame, config)
    assert len(records) == 1
    record = records[0]
    assert record.row == 2
    assert record.kind == "rule_cross_field"
    assert record.value == "delivered_date=2024-01-09, contract_date=2024-01-10"
    assert "'delivered_date' must be >= 'contract_date'" in record.reason


def test_held_row_set_only_counts_row_level_errors():
    config = Config.model_validate(
        {
            "fields": [
                {"name": "id", "type": "string", "required": True},
                {
                    "name": "amount",
                    "type": "float",
                    "rules": [{"rule": "range", "min": 0, "severity": "warning"}],
                },
            ]
        }
    )
    frame = _frame(id=["A", None], amount=[-5.0, 10.0])
    records = validate_frame(frame, config)
    assert held_row_set(records) == {("orders.csv", 3)}
