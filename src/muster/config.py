"""Configuration models and loading.

The configuration file (``muster.yaml``) declares the canonical schema, where
source files live, matching behaviour, safety limits and output locations. It
is parsed with ``yaml.safe_load`` only — no object construction from input.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

FieldType = Literal["string", "integer", "float", "boolean", "date", "datetime"]


class ConfigError(RuntimeError):
    """Raised when the configuration file is missing or invalid."""


class FieldSpec(BaseModel):
    """One field of the canonical schema."""

    name: str = Field(min_length=1)
    type: FieldType = "string"
    required: bool = False
    synonyms: list[str] = Field(default_factory=list)


class MatchingConfig(BaseModel):
    """Column matching behaviour."""

    fuzzy_threshold: float = Field(default=90, ge=0, le=100)


class LimitsConfig(BaseModel):
    """Safety limits applied to untrusted input."""

    max_file_size_mb: int = Field(default=100, ge=1)


class OutputConfig(BaseModel):
    """Where consolidated output and exceptions are written."""

    directory: Path = Path("output")
    dataset_name: str = Field(default="consolidated", min_length=1)


class Config(BaseModel):
    """Top-level Muster configuration."""

    fields: list[FieldSpec] = Field(min_length=1)
    sources: list[str] = Field(default=["**/*.csv", "**/*.xlsx"], min_length=1)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)


def load_config(path: Path) -> Config:
    """Load and validate a configuration file."""
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
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


CONFIG_TEMPLATE = """\
# muster.yaml — configuration for Muster.
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
#             exceptions.csv
#   synonyms: alternative headings this field is known by in source files;
#             matched case- and punctuation-insensitively
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
  - name: signup_date
    type: date
    required: false
    synonyms: ["date joined", "joined", "registration date"]
  - name: lifetime_value
    type: float
    required: false
    synonyms: ["ltv", "total spend"]
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

# Safety limits. Source files larger than this are skipped and recorded in
# exceptions.csv.
limits:
  max_file_size_mb: 100

# Where the consolidated dataset (Parquet and CSV) and exceptions.csv are
# written, relative to this file.
output:
  directory: output
  dataset_name: consolidated
"""
