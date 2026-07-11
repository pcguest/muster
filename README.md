# Muster

Muster consolidates inconsistent spreadsheets into one governed dataset. Point
it at a folder of CSV and XLSX files that disagree on column names, date
formats and number styles, and it maps every column onto a canonical schema
you define, coerces values to declared types, and writes a single consolidated
dataset. Anything it cannot map or coerce is written to `exceptions.csv` —
Muster never guesses silently and never drops data without a written
exception.

Input files are treated as untrusted: parsing is done with safe readers,
paths are confined to configured directories, and a configurable file size
limit applies.

## Install

Requires Python 3.11 or later.

```sh
pip install -e .
```

## Usage

```sh
# Write a starter muster.yaml with an example canonical schema
muster init

# Inspect a folder of spreadsheets: columns, inferred types, row counts and
# format inconsistencies, printed to the terminal and saved to profile.json
muster profile <folder>

# Run the pipeline: map, coerce and consolidate everything matched by the
# sources globs in muster.yaml into Parquet and CSV, plus exceptions.csv
muster run
```

Add `--verbose` before any command for detailed logging, e.g.
`muster --verbose run`.

## Licence

MIT — see [LICENSE](LICENSE).
