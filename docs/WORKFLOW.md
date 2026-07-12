# The workflow, start to finish

This page walks the whole workflow as a narrative: from a folder of
spreadsheets you did not create and do not trust, to a governed dataset
published on a schedule, with every decision along the way recorded. Try
each step on the built-in demo first (`muster demo` writes one and runs the
pipeline over it), or follow along with your own files.

## 1. Look before you configure: `muster profile`

```sh
muster profile sources/
```

Profiling reads every `.csv` and `.xlsx` file in the folder and reports,
per file, the columns, inferred types, non-empty counts and format
inconsistencies — mixed date formats, thousands separators, case variants —
as terminal tables and a `profile.json`. Nothing is written to your data;
this is reconnaissance. What you learn here (which headings vary, which
columns are dates in disguise) is exactly what the configuration has to
capture.

## 2. Propose a configuration: `muster init --from`

```sh
muster init --from sources/
```

Plain `muster init` writes a commented starter `muster.yaml` to edit by
hand. With `--from`, Muster profiles the folder and *proposes* one instead:
headings that look like variants of one another are clustered, the most
common variant becomes the canonical field name, types are inferred from
the values, and every observed heading is kept as a synonym.

The proposal is not the configuration of record. Every inference carries a
`# PROPOSED` marker with its rationale, and `muster run` refuses to serve
the file while any marker remains:

```sh
$EDITOR muster.yaml   # review each line; fix anything read wrongly
muster confirm        # then accept whatever is still marked
```

This is deliberate friction. A tool that infers your schema and quietly
runs with it has decided what your data means without asking; Muster makes
the human sign-off a mechanical requirement, not a convention. Every key
the file can hold is documented in [CONFIG.md](CONFIG.md).

## 3. Consolidate: `muster run`

```sh
muster run
```

One run reads every file the `sources` globs match (confined to the
project root, size-capped, de-duplicated), maps each column onto the
canonical schema — exact match, then synonyms, then fuzzy matching above
the configured threshold — coerces values to the declared types, validates
every row against the field and cross-field rules, and reconciles
duplicate keys across files.

Everything that cannot be handled honestly becomes a row in
`exceptions.csv` — unmapped and ambiguous columns, failed coercions,
missing required fields, rule violations, conflicting duplicates — each
with the file, row, column, offending value and reason, at a severity that
states the consequence: an **error** held the row out of the governed
dataset, a **warning** let it through and wrote it down. The exit code says
the same thing to automation: `0` clean, `2` error-severity exceptions
recorded, `1` the run could not proceed.

The outputs land beside your config: the governed dataset as Parquet and
CSV, `exceptions.csv`, `report.html`, and a manifest under `runs/` that
records the SHA-256 of the configuration, every input and every output,
chained to the manifest before it. The chain is why a Muster dataset can be
audited months later: any historic manifest that has been altered breaks
verification of everything after it.

## 4. Resolve what the machine could not: `muster review`

Two kinds of leftovers need a human, and both wait for one.

**Unmapped columns.** If fuzzy matching could not place a column, you can
add a synonym to `muster.yaml` and rerun — or, if `MUSTER_LLM_API_KEY` is
set, let a model propose targets:

```sh
muster run --assist     # writes proposals to mapping-review.yaml
muster review           # accept or reject each one, interactively
muster run              # accepted mappings now apply
```

Proposals carry confidence, rationale and the exact redacted samples that
were sent (only headings, inferred types and up to five redacted values
ever leave the machine — no cell data, no file names). A proposal does
nothing until accepted; rejected ones are kept in the file as a record.

**Exceptions.** `exceptions.csv` is the work queue for the data itself.
Fix the source files, adjust rules or survivorship, and rerun — or triage
them in the dashboard (step 6), where each resolve/dismiss decision appends
to an audit log without ever rewriting the run's records.

## 5. Read the report: `muster report`

Every run writes `report.html` — one self-contained file, no external
requests — with the run summary, per-field completeness and validity,
per-source-file quality scores, mapping decisions, exception counts and
the conflicts held for review. It is written for the person who owns the
data, not the person who ran the tool: send it to whoever has to decide
whether the month's numbers can be trusted. `muster report` re-renders it
for the latest run, or `--run <id>` for any past one, from data archived in
the run directory.

## 6. Watch it in a browser: `muster serve`

```sh
muster serve
```

The dashboard shows the latest run, per-field quality and trends across
runs, a filterable exceptions browser (resolve or dismiss with a note —
decisions append to `runs/resolutions.jsonl`; history is never mutated),
the mapping review flow with buttons, a run trigger, and the report
inline. It binds 127.0.0.1 by default and requires a login token generated
on first serve — the threat model, and the reasons behind each control,
are in [SECURITY.md](SECURITY.md).

## 7. Send it somewhere: `muster publish`

```sh
muster publish warehouse --dry-run   # print the plan; write nothing
muster publish warehouse
```

Publishing sends the latest governed dataset to a target configured under
`targets:` — SQLite, PostgreSQL, a REST endpoint, or Salesforce
([CONNECTORS.md](CONNECTORS.md) documents each). Before anything is sent,
the dataset on disk must hash to what the latest run's manifest recorded;
a run that recorded error-severity exceptions is refused unless you pass
`--force`, and a forced publish is written into the manifest chain in so
many words. Writes are idempotent (upserts on key columns or an External
ID), so republishing converges instead of duplicating, and every publish —
successful, partial, failed or forced — appends its own manifest to the
chain.

## 8. Make it routine: `muster schedule`

```sh
muster schedule "0 6 * * 1-5"   # weekdays at 06:00
muster daemon start
```

The schedule is a five-field cron expression stored beside `muster.yaml`;
the bundled daemon runs the pipeline on it, each run in a subprocess, with
a PID file and a rotating log under `runs/`, recording non-zero exits and
notifying a webhook (`MUSTER_WEBHOOK_URL`) on failure. Where a real
scheduler is available, prefer it — `muster schedule --print` emits a
ready-to-use systemd unit and crontab line, and the run's exit codes are
designed to fail loudly in cron and CI alike.

From here the loop is steps 3–7 on repeat: the pipeline runs on schedule,
exceptions surface in the dashboard, humans resolve them, and the manifest
chain quietly accumulates the evidence that every published number can be
traced to its source.
