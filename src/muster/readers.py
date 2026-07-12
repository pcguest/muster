"""Readers for untrusted CSV and XLSX sources.

Every column is returned as a string so that type coercion is explicit and
per-cell failures can be captured, rather than guessed at read time. XLSX
files are parsed with fastexcel (the Rust calamine reader), which does not
evaluate formulas or macros and is not an XML external entity vector. CSV is
parsed by Polars' native reader. Nothing here executes or deserialises
arbitrary code from input files.

Files are read in bounded chunks so peak memory does not scale with file
size. CSV chunks stream through Polars' lazy scanner. XLSX chunks are row
slices requested from calamine — that bounds the Polars frame held at once,
though calamine itself still walks the sheet per slice, so XLSX chunking
bounds memory rather than re-reading cost.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import fastexcel
import polars as pl

logger = logging.getLogger(__name__)

# Spreadsheet-style row numbers carried through the pipeline so exceptions
# can point at the source: the header is row 1, the first data row is row 2.
ROW_COLUMN = "_row"

DEFAULT_CHUNK_ROWS = 100_000


class ReaderError(RuntimeError):
    """Raised when a source file cannot be read as tabular data."""


def _csv_chunks(path: Path, chunk_rows: int) -> Iterator[pl.DataFrame]:
    lazy = pl.scan_csv(path, infer_schema=False, encoding="utf8-lossy")
    yielded = False
    for batch in lazy.collect_batches(chunk_size=chunk_rows):
        yielded = True
        yield batch
    if not yielded:
        # A header-only file yields no batches, but its headings still
        # matter for mapping; collect the empty frame for its columns.
        yield lazy.collect()


def _xlsx_chunks(path: Path, chunk_rows: int) -> Iterator[pl.DataFrame]:
    reader = fastexcel.read_excel(path)
    offset = 0
    while True:
        sheet = reader.load_sheet(0, skip_rows=offset, n_rows=chunk_rows)
        frame = sheet.to_polars().with_columns(pl.all().cast(pl.String))
        if frame.height == 0 and offset > 0:
            return
        yield frame
        if frame.height < chunk_rows:
            return
        offset += frame.height


def iter_table_chunks(
    path: Path, chunk_rows: int = DEFAULT_CHUNK_ROWS
) -> Iterator[pl.DataFrame]:
    """Yield string-typed chunks of a CSV or XLSX file, in file order.

    Each chunk carries a :data:`ROW_COLUMN` column of source row numbers,
    continuous across chunks. At least one chunk is always yielded for a
    readable file, so headings survive even when there are no data rows.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        chunks = _csv_chunks(path, chunk_rows)
    elif suffix == ".xlsx":
        chunks = _xlsx_chunks(path, chunk_rows)
    else:
        raise ReaderError(f"unsupported file type '{suffix}' for {path.name}")

    row_offset = 2
    checked = False
    try:
        for chunk in chunks:
            if not checked:
                if ROW_COLUMN in chunk.columns:
                    raise ReaderError(
                        f"{path.name} has a column named '{ROW_COLUMN}', which is reserved"
                    )
                checked = True
            logger.debug(
                "read chunk file=%s rows=%d from_row=%d", path.name, chunk.height, row_offset
            )
            yield chunk.with_row_index(ROW_COLUMN, offset=row_offset)
            row_offset += chunk.height
    except ReaderError:
        raise
    except Exception as exc:
        raise ReaderError(f"could not read {path.name}: {exc}") from exc


def read_table(path: Path) -> pl.DataFrame:
    """Read a whole CSV or XLSX file with every column as a string.

    Adds a :data:`ROW_COLUMN` column holding source row numbers. Chunked
    callers should prefer :func:`iter_table_chunks`.
    """
    frame = pl.concat(iter_table_chunks(path), how="vertical")
    logger.debug("read file=%s rows=%d columns=%d", path.name, frame.height, frame.width)
    return frame
