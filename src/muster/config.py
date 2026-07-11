"""Configuration models and loading.

The configuration file (``muster.yaml``) declares the canonical schema, where
source files live, matching behaviour, safety limits and output locations. It
is parsed with ``yaml.safe_load`` only — no object construction from input.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

FieldType = Literal["string", "integer", "float", "boolean", "date", "datetime"]
Severity = Literal["error", "warning"]
Operator = Literal["==", "!=", "<", "<=", ">", ">="]

_NUMERIC_TYPES = frozenset({"integer", "float"})
_TEMPORAL_TYPES = frozenset({"date", "datetime"})


class ConfigError(RuntimeError):
    """Raised when the configuration file is missing or invalid."""


class RangeRule(BaseModel):
    """Bound a numeric or temporal field. Temporal bounds are ISO strings."""

    rule: Literal["range"]
    min: float | str | None = None
    max: float | str | None = None
    severity: Severity = "error"

    @model_validator(mode="after")
    def _at_least_one_bound(self) -> RangeRule:
        if self.min is None and self.max is None:
            raise ValueError("range rule needs min, max or both")
        return self


class RegexRule(BaseModel):
    """Require a string field to match a regular expression in full."""

    rule: Literal["regex"]
    pattern: str = Field(min_length=1)
    severity: Severity = "error"

    @model_validator(mode="after")
    def _pattern_compiles(self) -> RegexRule:
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc
        return self


class AllowedValuesRule(BaseModel):
    """Restrict a field to a fixed set of values."""

    rule: Literal["allowed_values"]
    values: list[str | int | float] = Field(min_length=1)
    severity: Severity = "error"


FieldRule = Annotated[
    Union[RangeRule, RegexRule, AllowedValuesRule], Field(discriminator="rule")
]


class FieldSpec(BaseModel):
    """One field of the canonical schema."""

    name: str = Field(min_length=1)
    type: FieldType = "string"
    required: bool = False
    synonyms: list[str] = Field(default_factory=list)
    rules: list[FieldRule] = Field(default_factory=list)


class CrossFieldRule(BaseModel):
    """Compare two canonical fields row by row, e.g. delivered >= contracted.

    The comparison is a structured operator, never an evaluated expression.
    Rows where either side is empty are not violations; emptiness is the
    required flag's business.
    """

    field: str = Field(min_length=1)
    operator: Operator
    other: str = Field(min_length=1)
    severity: Severity = "error"


class SurvivorshipConfig(BaseModel):
    """How to resolve conflicting duplicate keys — only if explicitly set.

    ``newest_file`` keeps the row from the most recently modified source
    file; ``priority_list`` keeps the row from the earliest file in
    ``priority``; ``manual`` holds every conflicting row out for review.
    """

    strategy: Literal["newest_file", "priority_list", "manual"]
    priority: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _priority_matches_strategy(self) -> SurvivorshipConfig:
        if self.strategy == "priority_list" and not self.priority:
            raise ValueError("priority_list strategy needs a non-empty priority list")
        if self.strategy != "priority_list" and self.priority:
            raise ValueError("priority is only used with the priority_list strategy")
        return self


class ValidationConfig(BaseModel):
    """Dataset-level validation: duplicate keys, cross-field rules."""

    keys: list[str] = Field(default_factory=list)
    cross_field: list[CrossFieldRule] = Field(default_factory=list)
    survivorship: SurvivorshipConfig | None = None


class MatchingConfig(BaseModel):
    """Column matching behaviour."""

    fuzzy_threshold: float = Field(default=90, ge=0, le=100)


class LimitsConfig(BaseModel):
    """Safety limits applied to untrusted input."""

    max_file_size_mb: int = Field(default=100, ge=1)
    # Rows read from a source file at a time; bounds peak memory per file.
    chunk_rows: int = Field(default=100_000, ge=1)


class OutputConfig(BaseModel):
    """Where consolidated output and exceptions are written."""

    directory: Path = Path("output")
    dataset_name: str = Field(default="consolidated", min_length=1)


def parse_bound(
    bound: float | str, field_type: FieldType
) -> float | date | datetime:
    """Parse a range bound against the field's type; raise ValueError if unfit."""
    if field_type in _NUMERIC_TYPES:
        if isinstance(bound, str):
            raise ValueError(f"range bound {bound!r} must be a number for a {field_type} field")
        return float(bound)
    if field_type == "date":
        if not isinstance(bound, str):
            raise ValueError(f"range bound {bound!r} must be an ISO date string")
        return date.fromisoformat(bound)
    if field_type == "datetime":
        if not isinstance(bound, str):
            raise ValueError(f"range bound {bound!r} must be an ISO datetime string")
        return datetime.fromisoformat(bound)
    raise ValueError(f"range rules do not apply to {field_type} fields")


class Config(BaseModel):
    """Top-level Muster configuration."""

    fields: list[FieldSpec] = Field(min_length=1)
    sources: list[str] = Field(default=["**/*.csv", "**/*.xlsx"], min_length=1)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)

    @model_validator(mode="after")
    def _rules_fit_field_types(self) -> Config:
        names = {spec.name: spec for spec in self.fields}
        if len(names) != len(self.fields):
            raise ValueError("field names must be unique")
        for spec in self.fields:
            for rule in spec.rules:
                if isinstance(rule, RangeRule):
                    for bound in (rule.min, rule.max):
                        if bound is not None:
                            parse_bound(bound, spec.type)
                elif isinstance(rule, RegexRule) and spec.type != "string":
                    raise ValueError(
                        f"regex rule on '{spec.name}' needs a string field, not {spec.type}"
                    )
                elif isinstance(rule, AllowedValuesRule) and spec.type == "boolean":
                    raise ValueError(
                        f"allowed_values rule on '{spec.name}' is redundant for a boolean field"
                    )
        for key in self.validation.keys:
            if key not in names:
                raise ValueError(f"validation key '{key}' is not a declared field")
        for rule in self.validation.cross_field:
            for name in (rule.field, rule.other):
                if name not in names:
                    raise ValueError(f"cross-field rule refers to unknown field '{name}'")
            left, right = names[rule.field].type, names[rule.other].type
            comparable = left == right or {left, right} <= _NUMERIC_TYPES
            if not comparable:
                raise ValueError(
                    f"cross-field rule cannot compare {left} '{rule.field}' "
                    f"with {right} '{rule.other}'"
                )
        return self


# Marks an unreviewed inference in a generated configuration; see scaffold.py.
_PROPOSED_MARKER = re.compile(r"#\s*PROPOSED\b")


def load_config(path: Path) -> Config:
    """Load and validate a configuration file.

    A generated configuration still carrying PROPOSED markers is refused:
    auto-generation never silently becomes the configuration of record.
    """
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    text = path.read_text(encoding="utf-8")
    if _PROPOSED_MARKER.search(text):
        raise ConfigError(
            f"{path} still contains PROPOSED markers from 'muster init --from'; "
            "review each inference and delete its marker, or run 'muster confirm' "
            "to accept them all"
        )
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    try:
        config = Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration in {path}:\n{exc}") from exc
    logger.debug(
        "loaded config path=%s fields=%d sources=%s", path, len(config.fields), config.sources
    )
    return config


CONFIG_TEMPLATE = r"""# muster.yaml — configuration for Muster.
#
# Muster reads every file matched by `sources`, maps each column onto the
# canonical schema below, coerces values to the declared types, and writes a
# consolidated dataset. Anything it cannot map or coerce is written to
# exceptions.csv — nothing is guessed silently and nothing is dropped without
# a written exception.

# The canonical schema. Each field becomes one column in the consolidated
# output.
#   name:     canonical column name in the output dataset
#   type:     one of string, integer, float, boolean, date, datetime
#   required: when true, a source file lacking this column is recorded in
#             exceptions.csv, and a row with an empty value is held out of
#             the governed dataset
#   synonyms: alternative headings this field is known by in source files;
#             matched case- and punctuation-insensitively
#   rules:    validation rules checked per row. Kinds: range (min/max, for
#             numeric and date fields), regex (string fields, full match),
#             allowed_values. Each takes severity: error (the row is held
#             out of the governed dataset) or warning (published, reported).
fields:
  - name: customer_id
    type: string
    required: true
    synonyms: ["customer number", "cust id", "client id"]
  - name: full_name
    type: string
    required: true
    synonyms: ["name", "customer name", "client"]
  - name: email
    type: string
    required: false
    synonyms: ["e-mail", "email address"]
    rules:
      - rule: regex
        pattern: '^[^@\s]+@[^@\s]+\.[^@\s]+$'
        severity: warning
  - name: signup_date
    type: date
    required: false
    synonyms: ["date joined", "joined", "registration date"]
  - name: lifetime_value
    type: float
    required: false
    synonyms: ["ltv", "total spend"]
    rules:
      - rule: range
        min: 0
        severity: warning
  - name: active
    type: boolean
    required: false
    synonyms: ["is active"]

# Glob patterns, relative to this file, that locate the source spreadsheets.
# Hidden directories and the output directory are always skipped. Narrow
# these once your sources live in one place, e.g. "sources/*.xlsx".
sources:
  - "**/*.csv"
  - "**/*.xlsx"

# Column matching. Headings are matched exactly first, then against synonyms,
# then case- and punctuation-insensitively with fuzzy comparison; a fuzzy
# candidate must score at least fuzzy_threshold (0-100) to map. Columns that
# match nothing go to exceptions.csv.
matching:
  fuzzy_threshold: 90

# Dataset-level validation.
#   keys:        columns that identify a record. Duplicate keys are detected;
#                rows whose key columns are empty are held out.
#   cross_field: structured row-by-row comparisons between two fields, e.g.
#                { field: delivered_date, operator: ">=", other: contract_date,
#                  severity: error }. Never an evaluated expression.
#   survivorship: how to resolve the SAME key appearing in DIFFERENT files
#                with conflicting values. Muster never guesses: with no
#                strategy set, conflicting rows are held out for review.
#                  strategy: newest_file     keep the most recently modified
#                                            file's row
#                  strategy: priority_list   keep the row from the earliest
#                                            file in `priority`
#                  strategy: manual          always hold conflicts for review
validation:
  keys: ["customer_id"]
  cross_field: []
  # survivorship:
  #   strategy: priority_list
  #   priority: ["sources/master.xlsx", "sources/regional.csv"]

# Safety limits. Source files larger than max_file_size_mb are skipped and
# recorded in exceptions.csv. chunk_rows bounds how many rows are read from
# a file at a time, which bounds peak memory on large files.
limits:
  max_file_size_mb: 100
  chunk_rows: 100000

# Where the consolidated dataset (Parquet and CSV) and exceptions.csv are
# written, relative to this file.
output:
  directory: output
  dataset_name: consolidated
"""
