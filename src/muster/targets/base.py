"""Shared shapes and helpers for publish targets."""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Sequence

import polars as pl

from muster.credentials import redact_text


class TargetError(RuntimeError):
    """Raised when a target cannot publish. Messages are always redacted."""

    def __init__(self, message: str) -> None:
        super().__init__(redact_text(message))


@dataclass(frozen=True)
class RecordFailure:
    """One record (or batch of records) a target could not deliver."""

    key: str
    code: str
    message: str


@dataclass
class PublishOutcome:
    """What one publish attempt achieved."""

    destination: str  # human-readable, secret-free
    rows_sent: int = 0
    failures: list[RecordFailure] = field(default_factory=list)


class Target(ABC):
    """A place the governed dataset can be published to."""

    def __init__(self, name: str, keys: Sequence[str]) -> None:
        self.name = name
        self.keys = list(keys)

    @abstractmethod
    def describe(self) -> str:
        """One secret-free line naming the destination."""

    @abstractmethod
    def plan(self, frame: pl.DataFrame) -> list[str]:
        """Dry run: the exact writes a publish would perform, as prose."""

    @abstractmethod
    def publish(self, frame: pl.DataFrame) -> PublishOutcome:
        """Deliver the dataset. Raises :class:`TargetError` on total failure."""


def json_safe(value: object) -> object:
    """Convert a dataset value to something JSON can carry losslessly."""
    if isinstance(value, dt.datetime):
        return value.isoformat(sep="T")
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def iter_records(frame: pl.DataFrame) -> Iterator[dict[str, object]]:
    """Rows as JSON-safe dictionaries, in dataset order."""
    for row in frame.iter_rows(named=True):
        yield {name: json_safe(value) for name, value in row.items()}


def batched(records: list[dict[str, object]], size: int) -> Iterator[list[dict[str, object]]]:
    for start in range(0, len(records), size):
        yield records[start : start + size]


def quote_ident(name: str) -> str:
    """Quote an SQL identifier from configuration for SQLite or PostgreSQL."""
    if "\x00" in name:
        raise TargetError(f"identifier {name!r} contains a NUL byte")
    return '"' + name.replace('"', '""') + '"'


def key_of(record: dict[str, object], keys: Sequence[str]) -> str:
    """A human-readable key for one record, for failure reporting."""
    if not keys:
        return ""
    return ", ".join(str(record.get(key, "")) for key in keys)
