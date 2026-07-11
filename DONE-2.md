# Goal 2: validation, reconciliation, reporting and audit trail

Muster now takes rows from consolidation through to governance: every source
row is published, held with a written error, or superseded by a duplicate —
and every run leaves a report and a tamper-evident manifest behind.

## What exists

- **Validation engine** (`rules.py`, configured per field in `muster.yaml`):
  required (per-row emptiness), range with numeric or ISO date bounds, regex
  (full match, string fields), allowed values, and structured cross-field
  comparisons (e.g. `delivered_date >= contract_date`) — never an evaluated
  expression. Each violation lands in `exceptions.csv` with a severity:
  errors hold the row out of the governed dataset, warnings are published
  and reported. Config models validate that rules fit their field types
  (`config.py`).
- **Reconciliation** (`reconcile.py`, keyed on `validation.keys`): duplicate
  keys that agree wherever both rows hold a value are merged losslessly with
  a written warning; conflicting keys get a conflict exception listing each
  source's value and are held unless survivorship is explicitly configured —
  `newest_file`, `priority_list`, or `manual` (always hold). Ties and
  unlisted files are held, never guessed. Rows with empty key columns are
  held.
- **`muster report`** (`report.py`): one self-contained HTML file — inline
  CSS, no external requests, no scripts — with the run summary (rows in,
  published, held, superseded), per-field completeness and validity,
  per-source-file quality scores, mapping decisions, exception counts by
  kind and severity, and conflicts held for review. Dark instrument-panel
  styling with a print stylesheet. Every run archives its report data as
  JSON in the run directory, so past runs re-render without re-reading
  sources.
- **CI-friendly `muster run`**: prints the published/held summary, the
  report path and the manifest path; exits 0 on a clean run (warnings
  allowed), 2 when any error-severity exception was recorded, 1 when the
  run could not proceed.
- **Audit trail** (`manifest.py`): every run writes
  `runs/<timestamp>/manifest.json` with the SHA-256 of the configuration,
  every input and every output, plus row and exception counts and duration.
  Each manifest embeds the SHA-256 of its predecessor, forming a hash chain;
  `verify_chain` detects any altered, missing or reordered historic
  manifest. This is the integrity leg of the CIA triad, documented plainly
  in the README.

## Security carried through

Inputs remain untrusted: safe readers only, paths confined, size limits
enforced, no eval anywhere — cross-field rules and survivorship are
structured configuration, not expressions. Everything rendered into
report.html is HTML-escaped, so a hostile spreadsheet cell or column heading
cannot inject markup into the report (covered by tests).

## Verification

- 54 tests pass via `pytest`: rule engine units, all survivorship
  strategies, manifest hash chaining (including a tampering test), report
  metric and escaping smoke tests, and an end-to-end run over four fixtures
  with deliberate conflicts and rule violations.
- On the fixtures: 16 rows in, 11 published, 4 held (one cross-file
  conflict pair, two coercion failures), 1 superseded by a lossless merge;
  3 errors and 4 warnings in `exceptions.csv`; `muster run` exits 2;
  consecutive runs chain their manifests and `muster report` re-renders the
  latest run.

## Deliberately not started

LLM-assisted mapping, connectors, any UI and performance work — later goals
cover them.
