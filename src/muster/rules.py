"""Row-level validation of the consolidated dataset against configured rules.

Rules run after coercion, on typed columns. Each violation becomes an
exception record with the rule's severity: an error holds the row out of the
governed dataset, a warning is reported but the row is published. Empty cells
never violate range, regex or allowed-values rules — only the required flag
speaks about emptiness.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

import polars as pl

from muster.config import (
    AllowedValuesRule,
    Config,
    CrossFieldRule,
    FieldSpec,
    RangeRule,
    RegexRule,
    parse_bound,
)
from muster.records import ExceptionRecord

logger = logging.getLogger(__name__)

_OPERATORS: dict[str, Callable[[pl.Expr, pl.Expr], pl.Expr]] = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


def _violations(
    frame: pl.DataFrame,
    mask: pl.Expr,
    column: str,
    kind: str,
    severity: str,
    reason: str,
    value_of: Callable[[dict[str, object]], str | None] | None = None,
) -> list[ExceptionRecord]:
    """Turn a boolean expression into one exception record per offending row."""
    offending = frame.filter(mask)
    records = []
    for row in offending.iter_rows(named=True):
        value = value_of(row) if value_of else _cell_text(row[column])
        records.append(
            ExceptionRecord(
                file=row["_source_file"],
                row=int(row["_row"]),
                column=column,
                value=value,
                kind=kind,
                severity=severity,  # type: ignore[arg-type]
                reason=reason,
            )
        )
    return records


def _cell_text(value: object) -> str | None:
    return None if value is None else str(value)


def _range_mask(column: pl.Expr, rule: RangeRule, spec: FieldSpec) -> pl.Expr:
    mask = pl.lit(False)
    if rule.min is not None:
        mask = mask | (column < pl.lit(parse_bound(rule.min, spec.type)))
    if rule.max is not None:
        mask = mask | (column > pl.lit(parse_bound(rule.max, spec.type)))
    return column.is_not_null() & mask


def _range_reason(rule: RangeRule, spec: FieldSpec) -> str:
    bounds = []
    if rule.min is not None:
        bounds.append(f"min {rule.min}")
    if rule.max is not None:
        bounds.append(f"max {rule.max}")
    return f"out of range for field '{spec.name}' ({', '.join(bounds)})"


def validate_frame(frame: pl.DataFrame, config: Config) -> list[ExceptionRecord]:
    """Check every configured rule against the consolidated frame.

    ``frame`` must carry ``_source_file`` and ``_row`` provenance columns and
    one typed column per canonical field.
    """
    records: list[ExceptionRecord] = []

    for spec in config.fields:
        column = pl.col(spec.name)
        if spec.required:
            records.extend(
                _violations(
                    frame,
                    column.is_null(),
                    spec.name,
                    kind="rule_required",
                    severity="error",
                    reason=f"required field '{spec.name}' is empty",
                )
            )
        for rule in spec.rules:
            if isinstance(rule, RangeRule):
                mask = _range_mask(column, rule, spec)
                kind, reason = "rule_range", _range_reason(rule, spec)
            elif isinstance(rule, RegexRule):
                # Full match: anchor the configured pattern explicitly.
                mask = column.is_not_null() & ~column.str.contains(
                    f"^(?:{rule.pattern})$"
                )
                kind, reason = "rule_regex", (
                    f"value does not match pattern for field '{spec.name}'"
                )
            elif isinstance(rule, AllowedValuesRule):
                allowed = pl.Series(rule.values).cast(frame.schema[spec.name])
                mask = column.is_not_null() & ~column.is_in(allowed.implode())
                kind, reason = "rule_allowed_values", (
                    f"value not in allowed values for field '{spec.name}'"
                )
            else:  # defensive: the config union should make this unreachable
                raise ValueError(f"unknown rule {rule!r}")
            records.extend(
                _violations(frame, mask, spec.name, kind, rule.severity, reason)
            )

    for cross in config.validation.cross_field:
        left, right = pl.col(cross.field), pl.col(cross.other)
        holds = _OPERATORS[cross.operator](left, right)
        mask = left.is_not_null() & right.is_not_null() & ~holds
        reason = f"'{cross.field}' must be {cross.operator} '{cross.other}'"

        def _pair_text(row: dict[str, object], r: CrossFieldRule = cross) -> str:
            return (
                f"{r.field}={_cell_text(row[r.field])}, "
                f"{r.other}={_cell_text(row[r.other])}"
            )

        records.extend(
            _violations(
                frame,
                mask,
                cross.field,
                kind="rule_cross_field",
                severity=cross.severity,
                reason=reason,
                value_of=_pair_text,
            )
        )

    if records:
        logger.info("validation violations=%d", len(records))
    return records


def held_row_set(records: Sequence[ExceptionRecord]) -> set[tuple[str, int]]:
    """(file, row) pairs blocked from the governed dataset by error records."""
    return {
        (record.file, record.row)
        for record in records
        if record.severity == "error" and record.row is not None
    }
