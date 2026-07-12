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
from typing import Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from muster.credentials import ENV_NAME_RE

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
    RangeRule | RegexRule | AllowedValuesRule, Field(discriminator="rule")
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


class RedactionConfig(BaseModel):
    """How sample values are redacted before leaving the machine."""

    mask_digits: bool = True
    truncate: int = Field(default=24, ge=1)


class AssistConfig(BaseModel):
    """Optional LLM assistance for columns fuzzy matching cannot map.

    Off unless ``muster run --assist`` is used AND the MUSTER_LLM_API_KEY
    environment variable is set (the key never lives in this file). Only
    column headings, inferred types and up to ``max_samples`` redacted
    sample values are ever sent — no cell data and no file names leave the
    machine, and nothing is applied until a person accepts it.
    """

    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    base_url: str | None = None
    model: str = "claude-sonnet-5"
    max_samples: int = Field(default=5, ge=0, le=5)  # hard privacy ceiling
    timeout_seconds: int = Field(default=60, ge=1, le=600)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)

    @model_validator(mode="after")
    def _base_url_fits_provider(self) -> AssistConfig:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError(
                "openai_compatible provider needs base_url, e.g. https://api.openai.com/v1"
            )
        if self.base_url and not self.base_url.startswith(("https://", "http://")):
            raise ValueError("assist base_url must be an http(s) URL")
        return self

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return "https://api.anthropic.com"


class TargetBase(BaseModel):
    """Common shape of every publish target.

    Targets forbid unknown keys so a credential pasted into muster.yaml is
    rejected at load time instead of silently sitting in a config file:
    secrets are named by environment variables (``*_env`` fields) and
    resolved from the environment or the OS keyring at publish time.
    """

    model_config = ConfigDict(extra="forbid")

    # Key columns used for idempotent upserts; defaults to validation.keys.
    key_columns: list[str] = Field(default_factory=list)

    @field_validator(
        "*",
        mode="after",
        check_fields=False,
    )
    @classmethod
    def _env_fields_name_variables(cls, value: object, info: ValidationInfo) -> object:
        if info.field_name and info.field_name.endswith("_env"):
            if not isinstance(value, str) or not ENV_NAME_RE.match(value):
                raise ValueError(
                    f"{info.field_name} must name an environment variable "
                    "(upper-case letters, digits and underscores) — never put "
                    "the secret itself in the configuration file"
                )
        return value


class SqliteTarget(TargetBase):
    """Publish into a SQLite database file (Python standard library)."""

    type: Literal["sqlite"]
    path: Path = Path("published.db")
    table: str = Field(min_length=1)


class PostgresTarget(TargetBase):
    """Publish into PostgreSQL via psycopg 3, transactionally."""

    type: Literal["postgres"]
    table: str = Field(min_length=1)
    # The connection string (which may embed a password) is a secret.
    dsn_env: str = "MUSTER_PG_DSN"


class RestTarget(TargetBase):
    """POST the dataset in batches of JSON records to an HTTP endpoint."""

    type: Literal["rest"]
    url: str = Field(min_length=1)
    auth: Literal["bearer", "api_key", "none"] = "bearer"
    token_env: str = "MUSTER_REST_TOKEN"
    api_key_header: str = "X-API-Key"
    batch_size: int = Field(default=500, ge=1, le=10_000)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    max_retries: int = Field(default=5, ge=0, le=10)

    @model_validator(mode="after")
    def _url_is_http(self) -> RestTarget:
        if not self.url.startswith(("https://", "http://")):
            raise ValueError("rest target url must be an http(s) URL")
        return self


class SalesforceTarget(TargetBase):
    """Upsert records into a Salesforce object via the REST API.

    The object, the External ID field and the canonical-to-Salesforce field
    map are user configuration — Muster cannot know an org's schema. Records
    are sent through the sObject Collections endpoint in batches of up to
    200, and per-record failures are recorded with Salesforce error codes.
    """

    type: Literal["salesforce"]
    object: str = Field(min_length=1)
    external_id_field: str = Field(min_length=1)
    # canonical field name -> Salesforce API field name
    field_map: dict[str, str] = Field(min_length=1)
    login_url: str = "https://login.salesforce.com"
    auth_flow: Literal["client_credentials", "username_password"] = "client_credentials"
    api_version: str = Field(default="v62.0", pattern=r"^v\d+\.\d$")
    batch_size: int = Field(default=200, ge=1, le=200)
    timeout_seconds: int = Field(default=30, ge=1, le=600)
    max_retries: int = Field(default=5, ge=0, le=10)
    client_id_env: str = "MUSTER_SF_CLIENT_ID"
    client_secret_env: str = "MUSTER_SF_CLIENT_SECRET"
    username_env: str = "MUSTER_SF_USERNAME"
    password_env: str = "MUSTER_SF_PASSWORD"

    @model_validator(mode="after")
    def _login_url_is_https(self) -> SalesforceTarget:
        if not self.login_url.startswith(("https://", "http://")):
            raise ValueError("salesforce login_url must be an http(s) URL")
        if self.external_id_field not in self.field_map.values():
            raise ValueError(
                "field_map must map some canonical field onto the "
                f"external_id_field '{self.external_id_field}' so records can be upserted"
            )
        return self


TargetConfig = Annotated[
    SqliteTarget | PostgresTarget | RestTarget | SalesforceTarget,
    Field(discriminator="type"),
]

_TARGET_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


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
    assist: AssistConfig = Field(default_factory=AssistConfig)
    targets: dict[str, TargetConfig] = Field(default_factory=dict)

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
        for cross in self.validation.cross_field:
            for name in (cross.field, cross.other):
                if name not in names:
                    raise ValueError(f"cross-field rule refers to unknown field '{name}'")
            left, right = names[cross.field].type, names[cross.other].type
            comparable = left == right or {left, right} <= _NUMERIC_TYPES
            if not comparable:
                raise ValueError(
                    f"cross-field rule cannot compare {left} '{cross.field}' "
                    f"with {right} '{cross.other}'"
                )
        for target_name, target in self.targets.items():
            if not _TARGET_NAME_RE.match(target_name):
                raise ValueError(
                    f"target name '{target_name}' must start with a letter and use "
                    "only letters, digits, hyphens and underscores"
                )
            for key in target.key_columns:
                if key not in names:
                    raise ValueError(
                        f"target '{target_name}' key column '{key}' is not a declared field"
                    )
            if isinstance(target, SalesforceTarget):
                for canonical in target.field_map:
                    if canonical not in names:
                        raise ValueError(
                            f"target '{target_name}' maps unknown field '{canonical}'"
                        )
        return self

    def resolved_key_columns(self, target: TargetConfig) -> list[str]:
        """A target's upsert keys: its own, or the dataset validation keys."""
        return list(target.key_columns) or list(self.validation.keys)


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

# Optional LLM assistance for columns fuzzy matching cannot map. Used only
# by 'muster run --assist', and only when the MUSTER_LLM_API_KEY environment
# variable is set — the key never lives in this file, and without it the
# feature is simply unavailable. Privacy: only column headings, inferred
# types and up to max_samples redacted sample values are sent; cell data and
# file names never leave the machine. Proposals go to mapping-review.yaml
# with confidence and rationale, and nothing is applied until a person
# accepts it ('muster review', or edit the file).
assist:
  provider: anthropic          # or openai_compatible (then set base_url)
  # base_url: https://api.openai.com/v1
  model: claude-sonnet-5
  max_samples: 5               # hard ceiling of 5
  redaction:
    mask_digits: true          # digits become '#'
    truncate: 24               # samples are cut to this many characters

# Where the consolidated dataset (Parquet and CSV) and exceptions.csv are
# written, relative to this file.
output:
  directory: output
  dataset_name: consolidated

# Publish targets for 'muster publish <name>' — see docs/CONNECTORS.md.
# Secrets NEVER live in this file: each target names environment variables
# (or use the OS keyring: 'keyring set muster <NAME>'), and unknown keys are
# rejected so a pasted credential fails loudly at load time. key_columns
# defaults to validation.keys; upserts on those keys keep publishes
# idempotent. Every target supports 'muster publish --dry-run'.
# targets:
#   warehouse:
#     type: sqlite
#     path: warehouse.db          # relative to this file
#     table: customers
#   analytics:
#     type: postgres
#     table: customers
#     dsn_env: MUSTER_PG_DSN      # e.g. postgresql://user:pass@host/db
#   ingest_api:
#     type: rest
#     url: https://example.com/api/ingest
#     auth: bearer                # bearer | api_key | none
#     token_env: MUSTER_REST_TOKEN
#     batch_size: 500
#   crm:
#     type: salesforce
#     object: Contact
#     external_id_field: Customer_Id__c
#     field_map:                  # canonical field -> Salesforce API name
#       customer_id: Customer_Id__c
#       full_name: LastName
#     auth_flow: client_credentials
#     login_url: https://login.salesforce.com
"""
