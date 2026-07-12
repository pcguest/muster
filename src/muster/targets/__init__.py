"""Publish targets: where a governed dataset can be sent.

Each target is configured under ``targets:`` in muster.yaml and driven by
``muster publish <name>``. Secrets are never part of the configuration —
targets name environment variables, resolved through
:mod:`muster.credentials` (environment first, OS keyring second) and
redacted from every log line and message. All targets support dry-run, and
writes are idempotent: relational targets upsert on key columns, Salesforce
upserts on an External ID, and REST batches carry the key columns so an
idempotent endpoint can deduplicate resends.
"""

from __future__ import annotations

from pathlib import Path

from muster.config import (
    Config,
    PostgresTarget,
    RestTarget,
    SalesforceTarget,
    SqliteTarget,
    TargetConfig,
)
from muster.targets.base import PublishOutcome, RecordFailure, Target, TargetError


def build_target(name: str, spec: TargetConfig, config: Config, root: Path) -> Target:
    """Construct the runtime target for one configuration entry."""
    keys = config.resolved_key_columns(spec)
    if isinstance(spec, SqliteTarget):
        from muster.targets.sqlite import SqliteRuntime

        return SqliteRuntime(name, spec, keys, root)
    if isinstance(spec, PostgresTarget):
        from muster.targets.postgres import PostgresRuntime

        return PostgresRuntime(name, spec, keys)
    if isinstance(spec, RestTarget):
        from muster.targets.rest import RestRuntime

        return RestRuntime(name, spec, keys)
    if isinstance(spec, SalesforceTarget):
        from muster.targets.salesforce import SalesforceRuntime

        return SalesforceRuntime(name, spec, keys)
    raise TargetError(f"unknown target type for '{name}'")  # pragma: no cover


__all__ = [
    "PublishOutcome",
    "RecordFailure",
    "Target",
    "TargetError",
    "build_target",
]
