#!/usr/bin/env python3
"""Benchmark the Muster pipeline on synthetic data.

Generates a configurable number of customer rows spread over several CSV
files with clashing headings, mixed date formats, thousands separators and
a sprinkle of deliberate violations and duplicate keys, then times a full
``run_pipeline`` (read, map, coerce, validate, reconcile, publish, report,
manifest) over them.

Generation and the measured run happen in separate child processes so the
reported peak RSS is the pipeline's own high-water mark, not the
generator's. Method and honest results live in docs/PERFORMANCE.md.

Usage:
    python scripts/bench.py --rows 5000000 --files 4
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import polars as pl

# One field set, four heading dialects — the mapper earns its keep.
HEADINGS = [
    ["customer_id", "full_name", "email", "signup_date", "lifetime_value", "active"],
    ["Customer ID", "Full Name", "E-Mail", "Date Joined", "LTV", "Is Active"],
    ["CUSTOMER_ID", "FULL_NAME", "EMAIL", "SIGNUP_DATE", "LIFETIME_VALUE", "ACTIVE"],
    ["customer id", "name", "email address", "joined", "ltv", "active"],
]

CONFIG = """\
fields:
  - name: customer_id
    type: string
    required: true
  - name: full_name
    type: string
    required: true
    synonyms: ["name"]
  - name: email
    type: string
    synonyms: ["e-mail", "email address"]
    rules:
      - rule: regex
        pattern: '^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$'
        severity: warning
  - name: signup_date
    type: date
    synonyms: ["date joined", "joined"]
  - name: lifetime_value
    type: float
    synonyms: ["ltv"]
    rules:
      - rule: range
        min: 0
        severity: warning
  - name: active
    type: boolean
    synonyms: ["is active"]
sources:
  - "sources/*.csv"
validation:
  keys: ["customer_id"]
limits:
  chunk_rows: {chunk_rows}
"""

# Mixed date representations, including some month-name dates that take the
# per-cell fallback path — real folders have them, so the benchmark does.
_DATE_POOL = (
    [f"2023-{m:02d}-{d:02d}" for m, d in [(1, 9), (2, 14), (3, 3), (4, 21)]]
    + [f"{d:02d}/{m:02d}/2023" for m, d in [(5, 5), (6, 17), (7, 28), (8, 2)]]
    + ["09 Sep 2023", "30 Oct 2023"]
)

# Consecutive files overlap by this many keys with identical rows, so the
# reconciler merges agreeing duplicates like it would in the field.
_OVERLAP = 50


def generate_sources(workdir: Path, rows: int, files: int) -> None:
    sources = workdir / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    per_file = rows // files
    for index in range(files):
        headings = HEADINGS[index % len(HEADINGS)]
        offset = index * per_file - (_OVERLAP if index else 0)
        count = per_file + (_OVERLAP if index else 0)
        i = pl.int_range(offset, offset + count, eager=True).alias("i")
        frame = pl.select(
            i.cast(pl.String).str.pad_start(9, "0").alias("id_digits"),
            i,
        ).select(
            pl.format("C-{}", pl.col("id_digits")).alias(headings[0]),
            pl.format("Customer {}", pl.col("i")).alias(headings[1]),
            # roughly one email per thousand is deliberately invalid
            pl.when(pl.col("i") % 997 == 0)
            .then(pl.lit("not-an-email"))
            .otherwise(pl.format("user{}@example.com", pl.col("i")))
            .alias(headings[2]),
            (pl.col("i") % len(_DATE_POOL))
            .replace_strict(dict(enumerate(_DATE_POOL)), return_dtype=pl.String)
            .alias(headings[3]),
            # a few negative values trip the range rule; some carry
            # thousands separators the coercer must strip
            pl.when(pl.col("i") % 1499 == 0)
            .then(pl.lit("-12.50"))
            .when(pl.col("i") % 23 == 0)
            .then(pl.lit("1,234.56"))
            .otherwise(pl.format("{}.25", pl.col("i") % 9000))
            .alias(headings[4]),
            (pl.col("i") % 2 == 0)
            .cast(pl.String)
            .str.replace("true", "Yes")
            .str.replace("false", "no")
            .alias(headings[5]),
        )
        frame.write_csv(sources / f"customers_{index:02d}.csv")
    (workdir / "muster.yaml").write_text(
        CONFIG.format(chunk_rows=CHUNK_ROWS), encoding="utf-8"
    )


def run_measured(workdir: Path) -> dict:
    from muster.config import load_config
    from muster.pipeline import run_pipeline

    config_path = workdir / "muster.yaml"
    config = load_config(config_path)
    started = time.perf_counter()
    result = run_pipeline(config, workdir, config_path)
    elapsed = time.perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak *= 1024  # ru_maxrss is KiB on Linux, bytes on macOS
    return {
        "elapsed_seconds": round(elapsed, 2),
        "peak_rss_bytes": peak,
        "rows_in": result.rows_in,
        "rows_published": result.rows_published,
        "rows_held": result.rows_held,
        "rows_superseded": result.rows_superseded,
        "errors": result.error_count,
        "warnings": result.warning_count,
    }


CHUNK_ROWS = 100_000


def main(argv: list[str] | None = None) -> int:
    global CHUNK_ROWS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=5_000_000, help="total rows")
    parser.add_argument("--files", type=int, default=4, help="number of source files")
    parser.add_argument("--chunk-rows", type=int, default=100_000)
    parser.add_argument("--workdir", type=Path, default=None, help="keep work here")
    parser.add_argument("--stage", choices=["generate", "run"], default=None)
    args = parser.parse_args(argv)
    CHUNK_ROWS = args.chunk_rows

    if args.stage == "generate":
        generate_sources(args.workdir, args.rows, args.files)
        return 0
    if args.stage == "run":
        print(json.dumps(run_measured(args.workdir)))
        return 0

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="muster-bench-"))
    workdir.mkdir(parents=True, exist_ok=True)
    base = [sys.executable, str(Path(__file__).resolve())]
    common = [
        "--rows", str(args.rows),
        "--files", str(args.files),
        "--chunk-rows", str(args.chunk_rows),
        "--workdir", str(workdir),
    ]
    print(f"workdir: {workdir}")
    print(f"generating {args.rows} rows over {args.files} csv files…")
    subprocess.run(base + common + ["--stage", "generate"], check=True)
    size = sum(f.stat().st_size for f in (workdir / "sources").glob("*.csv"))
    print(f"source data: {size / 1e6:.0f} MB")
    print("running pipeline (measured in its own process)…")
    completed = subprocess.run(
        base + common + ["--stage", "run"], check=True, capture_output=True, text=True
    )
    stats = json.loads(completed.stdout.strip().splitlines()[-1])
    rate = stats["rows_in"] / stats["elapsed_seconds"] if stats["elapsed_seconds"] else 0
    print(
        f"\nrows in:        {stats['rows_in']:>12,}\n"
        f"published:      {stats['rows_published']:>12,}\n"
        f"held:           {stats['rows_held']:>12,}\n"
        f"superseded:     {stats['rows_superseded']:>12,}\n"
        f"exceptions:     {stats['errors']:,} error(s), {stats['warnings']:,} warning(s)\n"
        f"wall time:      {stats['elapsed_seconds']:>9.2f} s\n"
        f"throughput:     {rate:>12,.0f} rows/s\n"
        f"peak rss:       {stats['peak_rss_bytes'] / 2**30:>11.2f} GiB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
