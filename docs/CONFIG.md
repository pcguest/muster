# Configuration reference

Everything Muster does is driven by one file, `muster.yaml`, parsed with
`yaml.safe_load` only and validated by strict Pydantic models before any
work starts. An invalid file is refused with the exact validation error; a
generated file still carrying `# PROPOSED` markers is refused until every
inference has been reviewed (see [WORKFLOW.md](WORKFLOW.md)).

Paths in the file are relative to the file itself, so the folder holding
`muster.yaml` is the project root.

This page documents every key. Only `fields` is mandatory; everything else
has the defaults shown.

## `fields` (required)

The canonical schema — each entry becomes one column of the governed
dataset, in order. Field names must be unique.

```yaml
fields:
  - name: customer_id        # canonical column name in the output
    type: string             # string | integer | float | boolean | date | datetime
    required: true           # default false
    synonyms: ["cust id", "client id"]
    rules:
      - rule: regex
        pattern: '^C\d+$'
        severity: warning    # error | warning (default error)
```

- `name` — the column name in the consolidated output. Source headings are
  matched against it exactly, then against `synonyms`, then case- and
  punctuation-insensitively by fuzzy comparison.
- `type` — what values are coerced to. Coercion is strict: a cell that does
  not parse as the declared type becomes an exception record, never a
  silent null. Dates and datetimes accept the common unambiguous formats;
  genuinely ambiguous values are exceptions.
- `required` — when true, a source file lacking any column that maps to
  this field is recorded in `exceptions.csv`, and a row with an empty value
  here is held out of the governed dataset (severity error).
- `synonyms` — alternative headings the field is known by. Matching is
  case- and punctuation-insensitive, so `"Cust ID"`, `cust_id` and
  `cust-id` all hit the synonym `"cust id"`.
- `rules` — per-row validation, three kinds:

| rule | applies to | keys |
|---|---|---|
| `range` | integer, float, date, datetime | `min`, `max` (at least one; date/datetime bounds are ISO strings) |
| `regex` | string | `pattern` (must compile; matched in full, not searched) |
| `allowed_values` | any non-boolean | `values` (non-empty list) |

Every rule takes `severity`: an `error` holds the row out of the governed
dataset, a `warning` publishes the row but records the violation. Rules
that cannot fit their field's type (a regex on an integer, a numeric bound
on a date) are rejected at load time, not discovered mid-run.

## `sources`

```yaml
sources:            # default: ["**/*.csv", "**/*.xlsx"]
  - "sources/*.csv"
  - "sources/*.xlsx"
```

Glob patterns, relative to `muster.yaml`, locating the source spreadsheets.
Resolved paths (including symlink targets) must stay inside the project
root; hidden directories, the output directory and the runs directory are
always skipped. Files matched by more than one pattern are read once.

## `matching`

```yaml
matching:
  fuzzy_threshold: 90   # 0–100, default 90
```

A source heading that matches no field name or synonym exactly is compared
fuzzily (case- and punctuation-insensitive). A candidate must score at
least `fuzzy_threshold` to map — and must win clearly: a heading that
scores similarly against two fields is an *ambiguous match* exception, not
a guess. Columns that match nothing become exceptions and their values are
never published. Lower the threshold to catch more variants; raise it (or
add explicit synonyms) if it ever maps something it should not.

## `validation`

Dataset-level checks, applied after coercion.

```yaml
validation:
  keys: ["customer_id"]
  cross_field:
    - field: delivered_date
      operator: ">="        # == | != | < | <= | > | >=
      other: contract_date
      severity: error
  survivorship:
    strategy: newest_file   # newest_file | priority_list | manual
    # priority: ["sources/master.xlsx", "sources/regional.csv"]
```

- `keys` — the columns that identify a record (default: none). With keys
  set, duplicate keys across files are detected and reconciled, rows whose
  key columns are empty are held out, and publish targets default to
  upserting on these columns.
- `cross_field` — structured row-by-row comparisons between two declared
  fields. The two fields must have comparable types (the same type, or
  both numeric). The comparison is an operator from a fixed set — never an
  evaluated expression, so a crafted value cannot become code. Rows where
  either side is empty are not violations; emptiness is `required`'s
  business.
- `survivorship` — what to do when the *same key* appears in *different
  files* with conflicting values. Unset (the default), conflicting rows
  are held for review with a conflict exception listing each source's
  value — Muster never guesses. `newest_file` keeps the row from the most
  recently modified source file; `priority_list` keeps the row from the
  earliest file in `priority` (required for this strategy, and only for
  it); `manual` states the default explicitly. Ties, and files not on the
  priority list, are held, never guessed. Duplicate rows that *agree*
  wherever both hold a value are merged losslessly regardless of strategy,
  with a written warning.

## `limits`

Safety limits applied to untrusted input.

```yaml
limits:
  max_file_size_mb: 100   # default 100, minimum 1
  chunk_rows: 100000      # default 100000, minimum 1
```

- `max_file_size_mb` — a source file larger than this is skipped and
  recorded in `exceptions.csv` instead of read into memory.
- `chunk_rows` — how many rows are read from a file at a time; bounds peak
  memory for raw reads. The typed, consolidated dataset is still held in
  memory — see [PERFORMANCE.md](PERFORMANCE.md) for what that costs.

## `output`

```yaml
output:
  directory: output           # relative to muster.yaml
  dataset_name: consolidated
```

The governed dataset is written to `<directory>/<dataset_name>.parquet`
and `.csv`, alongside `exceptions.csv`, `report.html` and (after a
publish with per-record failures) `publish-exceptions.csv`. Run manifests
live separately under `runs/`.

## `assist` (optional feature, off by default)

LLM assistance for columns fuzzy matching cannot map. The feature only
runs when `muster run --assist` is used **and** the `MUSTER_LLM_API_KEY`
environment variable is set — the key never appears in this file, and
without it Muster is fully functional. Only column headings, inferred
types and up to `max_samples` *redacted* sample values are sent; cell data
and file names never leave the machine. Nothing a model proposes is
applied until a person accepts it (`muster review`).

```yaml
assist:
  provider: anthropic       # anthropic | openai_compatible
  # base_url: https://api.openai.com/v1   # required for openai_compatible
  model: claude-sonnet-5
  max_samples: 5            # 0–5; 5 is a hard ceiling, not a default to raise
  timeout_seconds: 60       # 1–600
  redaction:
    mask_digits: true       # digits in samples become '#'
    truncate: 24            # samples cut to this many characters (min 1)
```

## `targets`

Publish destinations for `muster publish <name>`, keyed by a name of your
choosing (letters, digits, hyphens, underscores; must start with a
letter). Full per-type documentation, including the Salesforce field map
and the idempotency contract, is in [CONNECTORS.md](CONNECTORS.md).

Three rules apply to every target:

- **Secrets never live here.** Fields ending in `_env` name environment
  variables (resolved from the environment or the OS keyring at publish
  time) and must look like environment variable names. Unknown keys in a
  target section are rejected at load time, so a pasted credential fails
  loudly instead of sitting in a config file.
- `key_columns` — the columns rows are upserted on; defaults to
  `validation.keys`. Every key column must be a declared field. With no
  keys anywhere, relational targets fully refresh the table instead.
- Every target supports `muster publish <name> --dry-run`.

```yaml
targets:
  warehouse:
    type: sqlite
    path: warehouse.db              # relative to muster.yaml
    table: receivals
  analytics:
    type: postgres
    table: receivals
    dsn_env: MUSTER_PG_DSN          # default shown
  ingest_api:
    type: rest
    url: https://example.com/api/ingest
    auth: bearer                    # bearer | api_key | none
    token_env: MUSTER_REST_TOKEN    # default shown
    api_key_header: X-API-Key       # used when auth: api_key
    batch_size: 500                 # 1–10000
    timeout_seconds: 30             # 1–600
    max_retries: 5                  # 0–10
  crm:
    type: salesforce
    object: Receival__c
    external_id_field: Ticket_Id__c
    field_map:                      # canonical field -> Salesforce API name;
      receival_id: Ticket_Id__c     # must cover the external_id_field
      grower: Grower_Name__c
    login_url: https://login.salesforce.com
    auth_flow: client_credentials   # client_credentials | username_password
    api_version: v62.0
    batch_size: 200                 # 1–200 (Salesforce's ceiling)
    timeout_seconds: 30
    max_retries: 5
    client_id_env: MUSTER_SF_CLIENT_ID          # defaults shown
    client_secret_env: MUSTER_SF_CLIENT_SECRET
    username_env: MUSTER_SF_USERNAME
    password_env: MUSTER_SF_PASSWORD
```

## Files beside muster.yaml that Muster manages

Not configuration, but part of the project's state:

| file | written by | purpose |
|---|---|---|
| `mapping-review.yaml` | `muster run --assist` | assist proposals awaiting human review |
| `muster.schedule` | `muster schedule` | the cron expression the daemon runs on |
| `.muster-token` | `muster serve` | dashboard login token, only when no OS keyring backend exists (owner-only permissions) |
| `runs/` | every run and publish | hash-chained manifests, archived report data, `resolutions.jsonl`, daemon PID file and log |
