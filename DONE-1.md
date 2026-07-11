# Goal 1: foundation

Muster's foundation is in place: a Python 3.11+ package (`src/` layout,
setuptools, MIT licence) installable with `pip install -e .`, exposing a
Typer CLI with three commands.

## What exists

- `muster init` writes a commented starter `muster.yaml`: a canonical schema
  (field names, types, required flags, synonyms), source globs, fuzzy
  matching threshold, file size limit and output paths, validated by
  Pydantic v2 models (`config.py`).
- `muster profile <folder>` scans `.csv`/`.xlsx` files and reports columns,
  inferred types, row counts and format inconsistencies (mixed date formats,
  thousands separators, case variants) as terminal tables and `profile.json`
  (`profiling.py`).
- `muster run` executes the pipeline (`pipeline.py`): read sources as
  string-typed Polars frames (`readers.py`), map columns via exact match,
  then synonyms, then case/punctuation-insensitive rapidfuzz matching with a
  configurable threshold (`mapping.py`), coerce to declared types with
  per-cell failure capture (`coercion.py`), and write consolidated Parquet
  and CSV plus `exceptions.csv` (`records.py`). Unmapped columns, failed
  coercions, ambiguous or duplicate matches, missing required fields,
  unreadable and oversized files all become exception rows with file, row,
  column, value and reason. Nothing is guessed silently; no data is dropped
  without a written exception.
- Structured logfmt-style logging behind `--verbose`; no `print()` in
  library code (`logs.py`).

## Security baseline

Input files are treated as untrusted: `yaml.safe_load` only, no eval or
pickle anywhere, XLSX parsed by fastexcel (calamine — no formula/macro
evaluation, no XXE surface), paths resolved and confined to the project root
(`security.py`), hidden and output directories skipped, and a configurable
file size limit enforced before reading. Dates are parsed per cell with
Python's `strptime` because Polars' vectorised parser can panic on malformed
strings — a bad cell must become an exception record, never a crash.

## Verification

- 20 tests pass via `pytest`: unit coverage for mapping, coercion and
  exception capture, plus an end-to-end run over three fixtures
  (two CSVs, one XLSX) that disagree on column names, date formats, number
  styles and boolean spellings.
- `muster init && muster profile tests/fixtures && muster run` succeeds from
  the repository root, consolidating 12 rows from 3 files with 3 recorded
  exceptions.

## Deliberately not started

Validation rules, reports, LLM features, connectors and any UI — later
goals cover them.
