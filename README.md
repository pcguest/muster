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

For a two-minute tour, `muster demo` generates three deliberately
disagreeing grain-receival spreadsheets (every value invented — no real
growers, sites or organisations) and runs the whole pipeline over them.

## Install

Requires Python 3.11 or later.

```sh
pip install -e .
```

## Usage

```sh
# Generate the built-in synthetic demo and run the pipeline over it
muster demo

# Write a starter muster.yaml with an example canonical schema
muster init

# Or propose a muster.yaml by profiling your real files (see below)
muster init --from <folder>
muster confirm

# Inspect a folder of spreadsheets: columns, inferred types, row counts and
# format inconsistencies, printed to the terminal and saved to profile.json
muster profile <folder>

# Run the pipeline: map, coerce, validate, reconcile and publish everything
# matched by the sources globs into Parquet and CSV, plus exceptions.csv,
# report.html and a run manifest
muster run

# Optionally ask an LLM to propose mappings for unmapped columns, then
# accept or reject each proposal yourself (see below)
muster run --assist
muster review

# Publish the latest governed dataset to a configured target — sqlite,
# postgres, a REST endpoint or Salesforce (see docs/CONNECTORS.md). Dry-run
# prints what would happen and writes nothing.
muster publish
muster publish warehouse --dry-run

# Re-render the HTML report for the latest run (or --run <id> for a past one)
muster report
```

Add `--verbose` before any command for detailed logging, e.g.
`muster --verbose run`.

## Generating a configuration from real files

`muster init --from <folder>` profiles the folder, clusters column headings
that look like variants of one another, and writes a proposed `muster.yaml`:
the most common heading variant becomes the canonical field name, types are
inferred from the values, and every observed heading is kept as a synonym.

Auto-generation never silently becomes the configuration of record. Every
inference is marked `# PROPOSED` with its rationale, and `muster run`
refuses to serve the file while any marker remains. Review each line and
delete its marker as you confirm it — or accept them all at once:

```sh
muster init --from sources
$EDITOR muster.yaml   # review; fix anything the profiler read wrongly
muster confirm        # accept whatever is still marked
```

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

## LLM-assisted mapping (optional, off by default)

When fuzzy matching cannot map a column, `muster run --assist` can ask an
LLM to propose a target. The feature only exists when the
`MUSTER_LLM_API_KEY` environment variable is set — the key never appears in
configuration or on disk, and without it Muster is fully functional and
simply reports the feature as unavailable.

**Privacy stance, stated plainly: no cell data leaves the machine.** A
request carries only column headings, one inferred type per column, and at
most five sample values that are redacted first — digits masked, length
truncated, both configurable. File names are not sent. The exact redacted
samples that were sent are recorded in the review file, so you can see
precisely what left the machine.

Nothing a model says is ever applied on its own. Proposals land in
`mapping-review.yaml` with confidence and rationale, marked `pending`, and
stay inert until a person accepts or rejects each one:

```sh
export MUSTER_LLM_API_KEY=...   # enables the feature; nothing else does
muster run --assist             # writes mapping-review.yaml
muster review                   # accept/reject each proposal interactively
muster run                      # accepted mappings now apply
```

`muster review --accept-all` and `--reject-all` cover the non-interactive
cases, and the file can simply be edited. The client is provider-agnostic:

```yaml
assist:
  provider: anthropic            # or openai_compatible (then set base_url)
  # base_url: https://api.openai.com/v1
  model: claude-sonnet-5
  max_samples: 5                 # hard ceiling of 5
  redaction:
    mask_digits: true            # digits become '#'
    truncate: 24                 # samples are cut to this many characters
```

## Performance

Muster consolidates and validates 5 million rows in under nine seconds at
~2 GiB peak memory on a laptop-class machine (Apple M2, 16 GB). Sources are
read in bounded chunks, coercion and validation run as vectorised Polars
expressions, and reconciliation only partitions duplicated keys. The
method, full numbers and the honest caveats — the typed dataset is held in
memory, so chunking bounds read buffers rather than the final frame — are
in [docs/PERFORMANCE.md](docs/PERFORMANCE.md); rerun `scripts/bench.py` on
your own hardware rather than trusting ours.

## Publishing

`muster publish [target]` sends the latest governed dataset to a target
configured under `targets:` in muster.yaml — a SQLite file, a PostgreSQL
table, a generic REST endpoint, or Salesforce (upsert on an External ID
field, with your own field mapping). Full configuration examples live in
[docs/CONNECTORS.md](docs/CONNECTORS.md).

```yaml
targets:
  warehouse:
    type: sqlite
    path: warehouse.db
    table: receivals
```

Publishing applies the CIA triad explicitly:

- **Confidentiality** — secrets never live in configuration, logs or
  output. Targets name environment variables (or use the OS keyring via the
  optional `keyring` library), unknown keys in a target section are
  rejected at load time, and every resolved secret is redacted from all
  output.
- **Integrity** — the dataset must hash to what the latest run's manifest
  recorded before anything is sent, and every publish (including failed and
  forced ones) appends to the tamper-evident manifest chain: target, row
  counts, duration, outcome.
- **Availability** — network targets retry transient failures with
  exponential backoff and jitter, honour `Retry-After` on 429, carry
  timeouts everywhere, and write idempotently (upserts on key columns, so a
  retried publish converges instead of duplicating).

A run that recorded error-severity exceptions is refused — an incomplete
dataset should not propagate downstream — unless you pass `--force`, which
is recorded loudly in the manifest chain. `--dry-run` prints exactly what
would happen and writes nothing. Per-record failures (for example
Salesforce error codes) land in `publish-exceptions.csv`, and the publish
exits with code 2 so automation notices.

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
exception counts and duration. Every publish appends its own manifest to
the same chain, recording the target, destination, source run, row counts
and outcome — including refusals overridden with `--force`. Each manifest
embeds the SHA-256 of the previous manifest, forming a tamper-evident hash
chain: altering any historic manifest breaks verification of every manifest
after it. This is the integrity leg of the CIA triad — the lineage of the
governed dataset, and of everywhere it was sent, can be checked, not merely
trusted.

## Licence

MIT — see [LICENSE](LICENSE).
