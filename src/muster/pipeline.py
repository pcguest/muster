"""The consolidation pipeline: discover, read, map, coerce, validate,
reconcile, publish.

Every source row is accounted for: published to the governed dataset, held
out by an error-severity exception, or superseded by a duplicate — each with
a written record in exceptions.csv. Every run writes an HTML report and a
hash-chained manifest under runs/. The pipeline never guesses silently and
never drops data without a written exception.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import polars as pl

from muster.coercion import TYPE_DTYPES, coerce_series
from muster.config import Config
from muster.manifest import (
    RUNS_DIRECTORY,
    create_run_directory,
    sha256_file,
    write_manifest,
)
from muster.mapping import ColumnMatch, map_columns
from muster.readers import ROW_COLUMN, ReaderError, iter_table_chunks
from muster.reconcile import reconcile
from muster.records import ExceptionRecord, count_by_severity, write_exceptions
from muster.report import REPORT_DATA_NAME, build_report, write_report
from muster.rules import held_row_set, validate_frame
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
    rows_in: int
    rows_published: int
    rows_held: int
    rows_superseded: int
    error_count: int
    warning_count: int
    output_parquet: Path
    output_csv: Path
    exceptions_csv: Path
    report_html: Path
    manifest_path: Path


def discover_sources(
    config: Config, root: Path
) -> tuple[list[Path], list[ExceptionRecord]]:
    """Expand the configured globs into a safe, de-duplicated file list.

    Files are confined to ``root``; hidden directories, the output directory
    and the runs directory are skipped. Oversized or traversing files are
    recorded as exceptions rather than read.
    """
    root = root.resolve()
    output_dir = (root / config.output.directory).resolve()
    runs_dir = (root / RUNS_DIRECTORY).resolve()
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
            if resolved.is_relative_to(output_dir) or resolved.is_relative_to(runs_dir):
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


def _coerce_chunk(
    chunk: pl.DataFrame,
    relative: str,
    config: Config,
    source_for: Mapping[str, str],
    exceptions: list[ExceptionRecord],
) -> pl.DataFrame:
    """Coerce one string-typed chunk into the internal typed schema."""
    row_numbers = chunk.get_column(ROW_COLUMN)
    columns: list[pl.Series] = [
        pl.Series(SOURCE_FILE_COLUMN, [relative] * chunk.height, dtype=pl.String),
        row_numbers.cast(pl.Int64).rename(ROW_COLUMN),
    ]
    for spec in config.fields:
        source = source_for.get(spec.name)
        if source is None:
            columns.append(
                pl.Series(spec.name, [None] * chunk.height, dtype=TYPE_DTYPES[spec.type])
            )
            continue
        original = chunk.get_column(source)
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
    return pl.DataFrame(columns)


def _consolidate_file(
    path: Path,
    relative: str,
    config: Config,
    exceptions: list[ExceptionRecord],
    mappings: list[tuple[str, ColumnMatch]],
) -> pl.DataFrame | None:
    """Map and coerce one file chunk by chunk; append its exceptions.

    Exceptions are buffered locally and only appended when the whole file
    reads cleanly, so a file that fails mid-read contributes exactly one
    ``file_unreadable`` record and no rows — never a partial mixture.
    """
    local: list[ExceptionRecord] = []
    local_mappings: list[tuple[str, ColumnMatch]] = []
    source_for: dict[str, str] = {}
    typed_chunks: list[pl.DataFrame] = []
    mapped = False
    try:
        for chunk in iter_table_chunks(path, config.limits.chunk_rows):
            if not mapped:
                mapped = True
                source_columns = [n for n in chunk.columns if n != ROW_COLUMN]
                matches = map_columns(
                    source_columns, config.fields, config.matching.fuzzy_threshold
                )
                local_mappings.extend((relative, match) for match in matches)
                source_for = {m.target: m.source for m in matches if m.target}
                for match in matches:
                    if match.target is None:
                        local.append(
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
                        local.append(
                            ExceptionRecord(
                                file=relative,
                                column=spec.name,
                                reason=f"required field '{spec.name}' not found in file",
                                kind="missing_required_column",
                            )
                        )
            typed_chunks.append(_coerce_chunk(chunk, relative, config, source_for, local))
    except ReaderError as exc:
        exceptions.append(
            ExceptionRecord(file=relative, reason=str(exc), kind="file_unreadable")
        )
        return None

    exceptions.extend(local)
    mappings.extend(local_mappings)
    frame = pl.concat(typed_chunks, how="vertical")
    logger.info(
        "consolidated file=%s rows=%d chunks=%d", relative, frame.height, len(typed_chunks)
    )
    return frame


def _internal_schema(config: Config) -> dict[str, pl.DataType]:
    return {SOURCE_FILE_COLUMN: pl.String(), ROW_COLUMN: pl.Int64()} | {
        spec.name: TYPE_DTYPES[spec.type] for spec in config.fields
    }


def _drop_held_rows(
    frame: pl.DataFrame, exceptions: list[ExceptionRecord]
) -> pl.DataFrame:
    """Remove rows blocked by error-severity, row-level exceptions."""
    held = held_row_set(exceptions)
    if not held or frame.height == 0:
        return frame
    held_frame = pl.DataFrame(
        {
            SOURCE_FILE_COLUMN: [file for file, _ in held],
            ROW_COLUMN: pl.Series([row for _, row in held], dtype=pl.Int64),
        }
    )
    return frame.join(held_frame, on=[SOURCE_FILE_COLUMN, ROW_COLUMN], how="anti")


def run_pipeline(config: Config, root: Path, config_path: Path) -> RunResult:
    """Run the full pipeline rooted at the configuration file's directory."""
    started_at = datetime.now(timezone.utc)
    root = root.resolve()
    files, exceptions = discover_sources(config, root)
    if not files and not exceptions:
        raise PipelineError(
            "no source files matched the configured globs: " + ", ".join(config.sources)
        )

    frames = []
    mappings: list[tuple[str, ColumnMatch]] = []
    file_rows: dict[str, int] = {}
    mtimes: dict[str, float] = {}
    inputs: list[tuple[str, Path, int]] = []
    for path in files:
        relative = str(path.relative_to(root))
        frame = _consolidate_file(path, relative, config, exceptions, mappings)
        rows = frame.height if frame is not None else 0
        file_rows[relative] = rows
        mtimes[relative] = path.stat().st_mtime
        inputs.append((relative, path, rows))
        if frame is not None:
            frames.append(frame)

    if frames:
        all_rows = pl.concat(frames, how="vertical")
    else:
        all_rows = pl.DataFrame(schema=_internal_schema(config))
    rows_in = all_rows.height

    # Rows already held by coercion errors never reach validation, so a
    # required field is only reported empty when the source cell was empty.
    candidates = _drop_held_rows(all_rows, exceptions)
    exceptions.extend(validate_frame(candidates, config))
    candidates = _drop_held_rows(candidates, exceptions)

    reconciled = reconcile(candidates, config, mtimes)
    exceptions.extend(reconciled.exceptions)
    published = reconciled.frame
    rows_published = published.height
    rows_held = rows_in - rows_published - reconciled.rows_superseded

    output_dir = (root / config.output.directory).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_parquet = output_dir / f"{config.output.dataset_name}.parquet"
    output_csv = output_dir / f"{config.output.dataset_name}.csv"
    exceptions_csv = output_dir / "exceptions.csv"
    report_html = output_dir / "report.html"

    governed = published.drop(ROW_COLUMN)
    governed.write_parquet(output_parquet)
    governed.write_csv(output_csv)
    write_exceptions(exceptions, exceptions_csv)

    run_dir = create_run_directory(root / RUNS_DIRECTORY, started_at)
    severities = count_by_severity(exceptions)
    report = build_report(
        config=config,
        published=published,
        file_rows=file_rows,
        mappings=mappings,
        exceptions=exceptions,
        run_id=run_dir.name,
        generated_at=started_at.isoformat(timespec="seconds"),
        config_file=config_path.name,
        config_sha256=sha256_file(config_path),
        duration_seconds=(datetime.now(timezone.utc) - started_at).total_seconds(),
        rows_in=rows_in,
        rows_published=rows_published,
        rows_held=rows_held,
        rows_superseded=reconciled.rows_superseded,
        conflicts_held=reconciled.conflicts_held,
    )
    write_report(report, report_html, run_dir / REPORT_DATA_NAME)

    manifest_path = write_manifest(
        run_dir,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        config_path=config_path,
        inputs=inputs,
        outputs={
            output_parquet.name: output_parquet,
            output_csv.name: output_csv,
            exceptions_csv.name: exceptions_csv,
            report_html.name: report_html,
            REPORT_DATA_NAME: run_dir / REPORT_DATA_NAME,
        },
        totals={
            "rows_in": rows_in,
            "rows_published": rows_published,
            "rows_held": rows_held,
            "rows_superseded": reconciled.rows_superseded,
            "errors": severities["error"],
            "warnings": severities["warning"],
        },
    )

    logger.info(
        "pipeline complete files=%d rows_in=%d published=%d held=%d exceptions=%d",
        len(frames),
        rows_in,
        rows_published,
        rows_held,
        len(exceptions),
    )
    return RunResult(
        files_read=len(frames),
        rows_in=rows_in,
        rows_published=rows_published,
        rows_held=rows_held,
        rows_superseded=reconciled.rows_superseded,
        error_count=severities["error"],
        warning_count=severities["warning"],
        output_parquet=output_parquet,
        output_csv=output_csv,
        exceptions_csv=exceptions_csv,
        report_html=report_html,
        manifest_path=manifest_path,
    )
