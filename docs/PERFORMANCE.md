# Performance

Muster is built to consolidate millions of spreadsheet rows on an ordinary
laptop. This page records how that is achieved, how it is measured, and the
actual numbers — including the caveats.

## How the pipeline stays fast and bounded

- **Chunked reading.** Source files are read `limits.chunk_rows` rows at a
  time (default 100,000). CSV chunks stream through Polars' lazy scanner;
  XLSX chunks are row slices requested from the calamine reader. Peak
  memory for raw string data is bounded by the chunk size, not the file
  size.
- **Vectorised coercion.** Numbers, booleans and every all-numeric date
  format are parsed by Polars' vectorised kernels. Month-name dates
  (`05 Mar 2024`) are the one exception: Polars' fast path for them can
  crash on untrusted value mixes, so cells that no numeric format accepts
  fall back to per-cell parsing — safe, and only as slow as the number of
  such cells.
- **Vectorised validation.** Rules evaluate as Polars expressions over
  whole columns. Only violating rows are materialised into exception
  records, so cost scales with violations, not rows.
- **Reconciliation skips the innocent.** Rows whose key is unique pass
  straight through; only duplicated keys are partitioned into groups for
  merge/conflict handling.

## What is *not* constant-memory, honestly

- The typed, consolidated dataset is held in memory across validation and
  reconciliation, so memory grows with total rows (typed columns are
  compact — see the numbers below). Chunking bounds the raw-read buffers,
  not the final dataset.
- XLSX chunking bounds the Polars frame per slice, but calamine still
  walks the sheet to serve each slice, and the format itself caps at
  1,048,576 rows per sheet.
- Exception records are Python objects: a run with hundreds of thousands
  of violations spends real time and memory materialising them. That is a
  data-quality signal, not a fast path.

## Method

`scripts/bench.py` generates N customer rows over several CSV files with
four heading dialects, mixed date formats (including ~20% month-name dates
that take the per-cell fallback), thousands separators, roughly one invalid
email per thousand rows, occasional negative values, and 50 duplicated keys
per file boundary that the reconciler must merge. It then runs the full
pipeline — read, map, coerce, validate, reconcile, publish Parquet + CSV +
exceptions + HTML report + hash-chained manifest — in a separate child
process and reports that process's wall time and peak RSS
(`resource.getrusage`), so the generator's memory is not counted.

Reproduce with:

```sh
python scripts/bench.py --rows 5000000 --files 4
```

## Results

Machine: MacBook Air, Apple M2 (8 cores), 16 GB RAM, macOS 26.3.1,
Python 3.14, Polars 1.42.1. Sources on the internal SSD. Recorded
2026-07-12 at commit `7833da8`.

| rows in   | source size | wall time | throughput   | peak RSS |
|-----------|-------------|-----------|--------------|----------|
| 1,000,150 | 75 MB       | 1.7 s     | ~592,000 r/s | 0.62 GiB |
| 5,000,150 | 376 MB      | 8.9 s     | ~564,000 r/s | 2.03 GiB |

Both runs published every clean row, merged 150 duplicate keys and recorded
~1,700 warnings per million rows (the deliberate bad emails and negative
values). Time scales close to linearly with rows; memory grows with the
dataset because the typed frame is held for validation and reconciliation,
as noted above.

Numbers will vary with machine, column count and how dirty the data is.
Rerun the script on your own hardware rather than trusting this table.
