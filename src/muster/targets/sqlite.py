"""SQLite publish target — Python standard library only.

The table is created if missing (with a UNIQUE constraint over the key
columns) and rows are upserted on those keys, so republishing the same
dataset is idempotent. With no key columns configured anywhere, the table
is fully refreshed instead. Either way the whole publish is one
transaction: all rows land, or none do.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import polars as pl

from muster.config import SqliteTarget
from muster.targets.base import (
    PublishOutcome,
    Target,
    TargetError,
    iter_records,
    quote_ident,
)

logger = logging.getLogger(__name__)


def _column_type(dtype: pl.DataType) -> str:
    if dtype == pl.Int64:
        return "INTEGER"
    if dtype == pl.Float64:
        return "REAL"
    if dtype == pl.Boolean:
        return "INTEGER"  # SQLite has no boolean; 0/1 round-trips cleanly
    return "TEXT"  # strings, dates and datetimes travel as text/ISO strings


class SqliteRuntime(Target):
    def __init__(
        self, name: str, spec: SqliteTarget, keys: Sequence[str], root: Path
    ) -> None:
        super().__init__(name, keys)
        self.spec = spec
        self.path = spec.path if spec.path.is_absolute() else root / spec.path

    def describe(self) -> str:
        return f"sqlite database {self.path}, table {self.spec.table}"

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
        placeholders = ", ".join("?" for _ in frame.columns)
        insert = f"INSERT INTO {table} ({names}) VALUES ({placeholders})"  # nosec B608
        if self.keys:
            conflict = ", ".join(quote_ident(k) for k in self.keys)
            updates = [
                f"{quote_ident(name)} = excluded.{quote_ident(name)}"
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
        create, insert = self._statements(frame)
        rows = [tuple(record.values()) for record in iter_records(frame)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.path)
        except sqlite3.Error as exc:
            raise TargetError(f"could not open sqlite database {self.path}: {exc}") from exc
        try:
            # The connection context manager commits on success and rolls the
            # whole transaction back on failure: all rows land, or none do.
            with connection:
                connection.execute(create)
                if not self.keys:
                    connection.execute(f"DELETE FROM {quote_ident(self.spec.table)}")  # nosec B608
                connection.executemany(insert, rows)
        except sqlite3.Error as exc:
            raise TargetError(
                f"sqlite publish to {self.path} failed and was rolled back: {exc}"
            ) from exc
        finally:
            connection.close()
        logger.info(
            "published target=%s rows=%d table=%s", self.name, len(rows), self.spec.table
        )
        return PublishOutcome(destination=self.describe(), rows_sent=len(rows))
