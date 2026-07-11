"""Readers for untrusted CSV and XLSX sources.

Every column is returned as a string so that type coercion is explicit and
per-cell failures can be captured, rather than guessed at read time. XLSX
files are parsed with fastexcel (the Rust calamine reader), which does not
evaluate formulas or macros and is not an XML external entity vector. CSV is
parsed by Polars' native reader. Nothing here executes or deserialises
arbitrary code from input files.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# Spreadsheet-style row numbers carried through the pipeline so exceptions
# can point at the source: the header is row 1, the first data row is row 2.
ROW_COLUMN = "_row"


class ReaderError(RuntimeError):
    """Raised when a source file cannot be read as tabular data."""


def read_table(path: Path) -> pl.DataFrame:
    """Read a CSV or XLSX file with every column as a string.

    Adds a :data:`ROW_COLUMN` column holding source row numbers.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            frame = pl.read_csv(path, infer_schema=False, encoding="utf8-lossy")
        elif suffix == ".xlsx":
            frame = pl.read_excel(path, engine="calamine")
            frame = frame.with_columns(pl.all().cast(pl.String))
        else:
            raise ReaderError(f"unsupported file type '{suffix}' for {path.name}")
    except ReaderError:
        raise
    except Exception as exc:
        raise ReaderError(f"could not read {path.name}: {exc}") from exc
    if ROW_COLUMN in frame.columns:
        raise ReaderError(
            f"{path.name} has a column named '{ROW_COLUMN}', which is reserved"
        )
    logger.debug("read file=%s rows=%d columns=%d", path.name, frame.height, frame.width)
    return frame.with_row_index(ROW_COLUMN, offset=2)
