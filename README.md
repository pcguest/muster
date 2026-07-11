# Muster

Muster consolidates inconsistent spreadsheets into one governed dataset. Point
it at a folder of CSV and XLSX files that disagree on column names, date
formats and number styles, and it maps every column onto a canonical schema
you define, coerces values to declared types, validates every row against
your rules, reconciles duplicate keys across files, and publishes a single
governed dataset. Anything it cannot map, coerce, validate or reconcile is
written to `exceptions.csv` — Muster never guesses silently and never drops
data without a written exception.

Input files are treated as untrusted: parsing is done with safe readers,
paths are confined to configured directories, a configurable file size limit
applies, and every value shown in the HTML report is escaped.

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

# Run the pipeline: map, coerce, validate, reconcile and publish everything
# matched by the sources globs into Parquet and CSV, plus exceptions.csv,
# report.html and a run manifest
muster run

# Re-render the HTML report for the latest run (or --run <id> for a past one)
muster report
```

Add `--verbose` before any command for detailed logging, e.g.
`muster --verbose run`.

`muster run` exits 0 on a clean run (warnings allowed), 2 if any
error-severity exceptions were recorded, and 1 if the run could not proceed —
so a CI job or cron entry fails loudly when the governed dataset is
incomplete.

## Validation

Rules live in `muster.yaml`. Each violation is recorded in `exceptions.csv`
with a severity: an **error** holds the row out of the governed dataset, a
**warning** is published but reported.

```yaml
fields:
  - name: email
    type: string
    rules:
      - rule: regex
        pattern: '^[^@\s]+@[^@\s]+\.[^@\s]+$'
        severity: warning
  - name: lifetime_value
    type: float
    rules:
      - rule: range          # numeric or date bounds; date bounds are ISO strings
        min: 0
        severity: warning
  - name: region
    type: string
    rules:
      - rule: allowed_values
        values: ["north", "south", "east", "west"]
        severity: error
```

Cross-field rules and duplicate detection are dataset-level. A cross-field
rule is a structured comparison — never an evaluated expression:

```yaml
validation:
  keys: ["customer_id"]      # duplicate detection and reconciliation key
  cross_field:
    - field: delivered_date
      operator: ">="
      other: contract_date
      severity: error
```

## Reconciliation

When the same key appears in more than one row, Muster never guesses. Rows
that agree wherever both hold a value are merged losslessly, with a written
warning. Rows that conflict get a conflict exception listing each source's
value, and are held for review unless you explicitly configure survivorship:

```yaml
validation:
  keys: ["customer_id"]
  survivorship:
    strategy: newest_file    # most recently modified source file wins
    # strategy: priority_list  # earliest file in `priority` wins
    # priority: ["sources/master.xlsx", "sources/regional.csv"]
    # strategy: manual         # always hold conflicts for review
```

## Report

Every run writes `report.html` — one self-contained file, no external
requests — showing the run summary (rows in, published, held), per-field
completeness and validity, per-source-file quality scores, mapping decisions,
exception counts by kind and severity, and conflicts held for review. It is
readable by non-technical reviewers and printable.

![Muster run report](docs/report-screenshot.png)

## Audit trail and integrity

Every run writes `runs/<timestamp>/manifest.json` recording the SHA-256 of
the configuration, of every input file and of every output, plus row and
exception counts and duration. Each manifest embeds the SHA-256 of the
previous run's manifest, forming a tamper-evident hash chain: altering any
historic manifest breaks verification of every manifest after it. This is
the integrity leg of the CIA triad — the lineage of the governed dataset can
be checked, not merely trusted.

## Licence

MIT — see [LICENSE](LICENSE).
