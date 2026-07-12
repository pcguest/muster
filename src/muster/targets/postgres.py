"""PostgreSQL publish target — psycopg 3, one transaction, all rows or none.

The connection string is a secret (it usually embeds a password): it is
named by ``dsn_env``, resolved from the environment or the OS keyring at
publish time, registered for redaction, and never echoed. The table is
created if missing with a UNIQUE constraint over the key columns and rows
are upserted (``INSERT … ON CONFLICT … DO UPDATE``), so republishing is
idempotent; with no keys the table is fully refreshed. psycopg is an
optional dependency (``pip install muster[postgres]``), imported lazily.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import polars as pl

from muster.config import PostgresTarget
from muster.credentials import resolve_secret
from muster.targets.base import (
    PublishOutcome,
    Target,
    TargetError,
    quote_ident,
)

logger = logging.getLogger(__name__)


def _connect(dsn: str) -> Any:  # patched in tests; never called against a live server
    try:
        import psycopg  # noqa: PLC0415 — optional dependency, imported lazily
    except ImportError as exc:
        raise TargetError(
            "the postgres target needs psycopg: pip install 'muster[postgres]'"
        ) from exc
    return psycopg.connect(dsn)


def _column_type(dtype: pl.DataType) -> str:
    if dtype == pl.Int64:
        return "BIGINT"
    if dtype == pl.Float64:
        return "DOUBLE PRECISION"
    if dtype == pl.Boolean:
        return "BOOLEAN"
    if dtype == pl.Date:
        return "DATE"
    if isinstance(dtype, pl.Datetime):
        return "TIMESTAMP"
    return "TEXT"


class PostgresRuntime(Target):
    def __init__(self, name: str, spec: PostgresTarget, keys: Sequence[str]) -> None:
        super().__init__(name, keys)
        self.spec = spec

    def describe(self) -> str:
        # The DSN is a secret; name only what identifies the destination.
        return (
            f"postgres table {self.spec.table} "
            f"(connection from {self.spec.dsn_env})"
        )

    def _statements(self, frame: pl.DataFrame) -> tuple[str, str]:
        table = quote_ident(self.spec.table)
        columns = [
            f"{quote_ident(name)} {_column_type(dtype)}"
            for name, dtype in frame.schema.items()
        ]
        constraint = (
            f", UNIQUE ({', '.join(quote_ident(k) for k in self.keys)})"
            if self.keys
            else ""
        )
        # Identifiers are quoted via quote_ident and come from the user's own
        # configuration; every value is parameterised.
        create = f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)}{constraint})"  # nosec B608
        names = ", ".join(quote_ident(name) for name in frame.columns)
        placeholders = ", ".join("%s" for _ in frame.columns)
        insert = f"INSERT INTO {table} ({names}) VALUES ({placeholders})"  # nosec B608
        if self.keys:
            conflict = ", ".join(quote_ident(k) for k in self.keys)
            updates = [
                f"{quote_ident(name)} = EXCLUDED.{quote_ident(name)}"
                for name in frame.columns
                if name not in self.keys
            ]
            # Identifiers are quoted via quote_ident; values are parameterised.
            action = (
                f"DO UPDATE SET {', '.join(updates)}" if updates else "DO NOTHING"  # nosec B608
            )
            insert += f" ON CONFLICT ({conflict}) {action}"
        return create, insert

    def plan(self, frame: pl.DataFrame) -> list[str]:
        lines = [
            f"connect to {self.describe()}",
            f"create table {self.spec.table} if missing ({frame.width} column(s))",
        ]
        if self.keys:
            lines.append(
                f"upsert {frame.height} row(s) on key ({', '.join(self.keys)}) "
                "in one transaction"
            )
        else:
            lines.append(
                f"replace the table's rows with {frame.height} row(s) in one "
                "transaction (no key columns configured)"
            )
        return lines

    def publish(self, frame: pl.DataFrame) -> PublishOutcome:
        dsn = resolve_secret(self.spec.dsn_env, f"postgres target '{self.name}'")
        create, insert = self._statements(frame)
        rows = list(frame.iter_rows())  # psycopg takes dates and datetimes natively
        try:
            connection = _connect(dsn)
        except TargetError:
            raise
        except Exception as exc:
            raise TargetError(f"could not connect for target '{self.name}': {exc}") from exc
        try:
            # One transaction: committed on success, rolled back on any error.
            with connection:
                with connection.cursor() as cursor:
                    cursor.execute(create)
                    if not self.keys:
                        cursor.execute(f"DELETE FROM {quote_ident(self.spec.table)}")  # nosec B608
                    cursor.executemany(insert, rows)
        except Exception as exc:
            raise TargetError(
                f"postgres publish to table {self.spec.table} failed and was "
                f"rolled back: {exc}"
            ) from exc
        finally:
            connection.close()
        logger.info(
            "published target=%s rows=%d table=%s", self.name, len(rows), self.spec.table
        )
        return PublishOutcome(destination=self.describe(), rows_sent=len(rows))
