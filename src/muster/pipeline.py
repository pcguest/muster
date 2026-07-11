"""The consolidation pipeline: discover, read, map, coerce, write.

Every source row lands in the consolidated dataset; every column that cannot
be mapped, cell that cannot be coerced, and file that cannot be read is
written to exceptions.csv with a reason. The pipeline never guesses silently
and never drops data without a written exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from muster.coercion import TYPE_DTYPES, coerce_series
from muster.config import Config
from muster.mapping import map_columns
from muster.readers import ROW_COLUMN, ReaderError, read_table
from muster.records import ExceptionRecord, write_exceptions
from muster.security import (
    SUPPORTED_SUFFIXES,
    SecurityError,
    ensure_size_within,
    ensure_within,
)

logger = logging.getLogger(__name__)

SOURCE_FILE_COLUMN = "_source_file"


class PipelineError(RuntimeError):
    """Raised when the pipeline cannot proceed at all."""


@dataclass(frozen=True)
class RunResult:
    files_read: int
    rows_out: int
    exception_count: int
    output_parquet: Path
    output_csv: Path
    exceptions_csv: Path


def discover_sources(
    config: Config, root: Path
) -> tuple[list[Path], list[ExceptionRecord]]:
    """Expand the configured globs into a safe, de-duplicated file list.

    Files are confined to ``root``; hidden directories and the output
    directory are skipped. Oversized or traversing files are recorded as
    exceptions rather than read.
    """
    root = root.resolve()
    output_dir = (root / config.output.directory).resolve()
    files: list[Path] = []
    exceptions: list[ExceptionRecord] = []
    seen: set[Path] = set()
    for pattern in config.sources:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            try:
                resolved = ensure_within(path, root)
            except SecurityError as exc:
                exceptions.append(
                    ExceptionRecord(file=str(path), reason=str(exc), kind="file_skipped")
                )
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            relative = resolved.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                logger.debug("skipped hidden path=%s", relative)
                continue
            if resolved.is_relative_to(output_dir):
                logger.debug("skipped output path=%s", relative)
                continue
            if resolved.suffix.lower() not in SUPPORTED_SUFFIXES:
                exceptions.append(
                    ExceptionRecord(
                        file=str(relative),
                        reason=f"unsupported file type '{resolved.suffix}'",
                        kind="file_skipped",
                    )
                )
                continue
            try:
                ensure_size_within(resolved, config.limits.max_file_size_mb)
            except SecurityError as exc:
                exceptions.append(
                    ExceptionRecord(file=str(relative), reason=str(exc), kind="file_skipped")
                )
                continue
            files.append(resolved)
    logger.info("discovered files=%d exceptions=%d", len(files), len(exceptions))
    return files, exceptions


def _consolidate_file(
    path: Path,
    relative: str,
    config: Config,
    exceptions: list[ExceptionRecord],
) -> pl.DataFrame | None:
    """Map and coerce one file; append its exceptions; return its rows."""
    try:
        frame = read_table(path)
    except ReaderError as exc:
        exceptions.append(
            ExceptionRecord(file=relative, reason=str(exc), kind="file_unreadable")
        )
        return None

    source_columns = [name for name in frame.columns if name != ROW_COLUMN]
    matches = map_columns(source_columns, config.fields, config.matching.fuzzy_threshold)
    source_for = {m.target: m.source for m in matches if m.target}

    for match in matches:
        if match.target is None:
            exceptions.append(
                ExceptionRecord(
                    file=relative,
                    column=match.source,
                    reason=f"unmapped column: {match.reason}",
                    kind="unmapped_column",
                    severity="warning",
                )
            )
    for spec in config.fields:
        if spec.required and spec.name not in source_for:
            exceptions.append(
                ExceptionRecord(
                    file=relative,
                    column=spec.name,
                    reason=f"required field '{spec.name}' not found in file",
                    kind="missing_required_column",
                )
            )

    row_numbers = frame.get_column(ROW_COLUMN)
    columns: list[pl.Series] = [
        pl.Series(SOURCE_FILE_COLUMN, [relative] * frame.height, dtype=pl.String)
    ]
    for spec in config.fields:
        source = source_for.get(spec.name)
        if source is None:
            columns.append(
                pl.Series(spec.name, [None] * frame.height, dtype=TYPE_DTYPES[spec.type])
            )
            continue
        original = frame.get_column(source)
        coerced, failed = coerce_series(original, spec.type)
        for index in failed.arg_true().to_list():
            exceptions.append(
                ExceptionRecord(
                    file=relative,
                    row=int(row_numbers[index]),
                    column=source,
                    value=original[index],
                    reason=f"cannot coerce to {spec.type} for field '{spec.name}'",
                    kind="coercion",
                )
            )
        columns.append(coerced.rename(spec.name))

    logger.info("consolidated file=%s rows=%d", relative, frame.height)
    return pl.DataFrame(columns)


def run_pipeline(config: Config, root: Path) -> RunResult:
    """Run the full pipeline rooted at the configuration file's directory."""
    root = root.resolve()
    files, exceptions = discover_sources(config, root)
    if not files and not exceptions:
        raise PipelineError(
            "no source files matched the configured globs: " + ", ".join(config.sources)
        )

    frames = []
    for path in files:
        relative = str(path.relative_to(root))
        frame = _consolidate_file(path, relative, config, exceptions)
        if frame is not None:
            frames.append(frame)

    output_dir = (root / config.output.directory).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_parquet = output_dir / f"{config.output.dataset_name}.parquet"
    output_csv = output_dir / f"{config.output.dataset_name}.csv"
    exceptions_csv = output_dir / "exceptions.csv"

    if frames:
        consolidated = pl.concat(frames, how="vertical")
    else:
        schema = {SOURCE_FILE_COLUMN: pl.String()} | {
            spec.name: TYPE_DTYPES[spec.type] for spec in config.fields
        }
        consolidated = pl.DataFrame(schema=schema)

    consolidated.write_parquet(output_parquet)
    consolidated.write_csv(output_csv)
    write_exceptions(exceptions, exceptions_csv)
    logger.info(
        "pipeline complete files=%d rows=%d exceptions=%d",
        len(frames),
        consolidated.height,
        len(exceptions),
    )
    return RunResult(
        files_read=len(frames),
        rows_out=consolidated.height,
        exception_count=len(exceptions),
        output_parquet=output_parquet,
        output_csv=output_csv,
        exceptions_csv=exceptions_csv,
    )
