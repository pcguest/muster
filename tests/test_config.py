"""Unit tests for validation-rule configuration models."""

import pytest
from pydantic import ValidationError

from muster.config import CONFIG_TEMPLATE, Config


def _config(**overrides):
    base = {
        "fields": [
            {"name": "id", "type": "string", "required": True},
            {"name": "amount", "type": "float"},
            {"name": "ordered", "type": "date"},
            {"name": "delivered", "type": "date"},
        ]
    }
    return Config.model_validate(base | overrides)


def test_template_parses_with_rules_and_validation():
    import yaml

    config = Config.model_validate(yaml.safe_load(CONFIG_TEMPLATE))
    assert config.validation.keys == ["customer_id"]
    assert config.validation.survivorship is None
    email = next(f for f in config.fields if f.name == "email")
    assert email.rules[0].rule == "regex"
    assert email.rules[0].severity == "warning"


def test_range_rule_accepts_iso_bounds_on_date_fields():
    config = _config(
        fields=[{"name": "d", "type": "date", "rules": [{"rule": "range", "min": "2020-01-01"}]}]
    )
    assert config.fields[0].rules[0].min == "2020-01-01"


def test_range_rule_rejects_non_numeric_bound_on_float_field():
    with pytest.raises(ValidationError, match="must be a number"):
        _config(
            fields=[{"name": "amount", "type": "float", "rules": [{"rule": "range", "min": "low"}]}]
        )


def test_range_rule_needs_at_least_one_bound():
    with pytest.raises(ValidationError, match="min, max or both"):
        _config(fields=[{"name": "amount", "type": "float", "rules": [{"rule": "range"}]}])


def test_regex_rule_rejected_on_non_string_field():
    with pytest.raises(ValidationError, match="needs a string field"):
        _config(fields=[{"name": "amount", "type": "float", "rules": [{"rule": "regex", "pattern": "x"}]}])


def test_regex_rule_rejects_invalid_pattern():
    with pytest.raises(ValidationError, match="invalid regex"):
        _config(fields=[{"name": "id", "rules": [{"rule": "regex", "pattern": "("}]}])


def test_validation_keys_must_be_declared_fields():
    with pytest.raises(ValidationError, match="not a declared field"):
        _config(validation={"keys": ["missing"]})


def test_cross_field_rule_requires_comparable_types():
    with pytest.raises(ValidationError, match="cannot compare"):
        _config(
            validation={
                "cross_field": [{"field": "id", "operator": ">=", "other": "amount"}]
            }
        )
    config = _config(
        validation={
            "cross_field": [{"field": "delivered", "operator": ">=", "other": "ordered"}]
        }
    )
    assert config.validation.cross_field[0].operator == ">="


def test_priority_list_strategy_needs_priority():
    with pytest.raises(ValidationError, match="non-empty priority list"):
        _config(validation={"keys": ["id"], "survivorship": {"strategy": "priority_list"}})
    config = _config(
        validation={
            "keys": ["id"],
            "survivorship": {"strategy": "priority_list", "priority": ["a.csv"]},
        }
    )
    assert config.validation.survivorship.strategy == "priority_list"
