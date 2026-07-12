# Goal 4: connectors — publishing the governed dataset

Muster now publishes the governed dataset to SQLite, PostgreSQL, generic
REST endpoints and Salesforce, with the CIA triad applied explicitly:
secrets never touch configuration or output, the manifest chain extends to
cover every publish, and network writes retry with backoff, carry timeouts
and stay idempotent.

## What exists

- **Secret handling** (`credentials.py`): target configurations name
  environment variables — never values — and target sections reject
  unknown keys, so a credential pasted into muster.yaml fails loudly at
  load time. Secrets resolve from the environment or, optionally, the OS
  keyring (`keyring set muster <NAME>`); every resolved secret (including
  OAuth access tokens returned mid-publish) is registered in-process and
  redacted from log lines, error messages, terminal output and manifests,
  longest first so a password inside a DSN cannot leave a fragment behind.
- **Four targets** (`targets/`), configured under `targets:` and all
  supporting `--dry-run` (prints the exact plan, writes nothing, not even
  a manifest entry). Key columns default to `validation.keys`:
  - *sqlite* (standard library) and *postgres* (psycopg 3, optional
    `muster[postgres]` extra, imported lazily): table created if missing
    with a UNIQUE constraint over the keys, rows upserted on those keys —
    or the table fully refreshed when no keys exist — in one transaction:
    all rows land, or none do.
  - *rest*: JSON record batches with bearer/API-key auth from the
    environment, configurable batch size, exponential backoff with jitter
    on 429/5xx/network failures, `Retry-After` honoured, and every unsent
    row accounted for when a batch fails hard. Idempotency is documented
    as the endpoint's half of the contract: records carry the key columns
    and retried batches are byte-identical.
  - *salesforce*: OAuth2 client-credentials or username-password flow,
    then sObject Collections upsert (`allOrNone: false`, batches ≤ 200) on
    a **user-configured** External ID field with a **user-configured**
    canonical-to-Salesforce field map; per-record failures land in
    exceptions with their Salesforce error codes, and rows lacking an
    External ID value are recorded rather than sent.
- **`muster publish [target]`** (`publish.py`): publishes the latest run's
  dataset after verifying it hashes to what that run's manifest recorded
  (--force does not override integrity); refuses a run holding
  error-severity exceptions unless `--force`, and a forced publish is
  written into the manifest chain in so many words. Every publish —
  published, partial, failed or forced — appends a `kind: publish`
  manifest to the tamper-evident chain: target, destination, source run,
  row counts, duration, outcome. Per-record failures go to
  `publish-exceptions.csv` and the command exits 2 so automation notices.
- The demo gained a `warehouse` sqlite target, so the whole story —
  refusal on the demo's deliberate errors, `--dry-run`, `--force`,
  idempotent republish — can be walked through end to end.

## Verification

- 112 tests pass via `pytest`: secret resolution (env and a faked
  keyring), redaction in logs and error text, sqlite publish end to end
  with idempotent upserts, refusal-then-force with the override visible in
  the verified chain, dry-run writing nothing, tampered-dataset refusal,
  chain verification across mixed run/publish manifests, postgres SQL
  shape and rollback against a fake connection, REST batching/auth
  headers/backoff windows/Retry-After/hard-rejection accounting, and
  Salesforce token flows, upsert payload shape, the 200-record ceiling,
  per-record error codes reaching publish-exceptions.csv, and credentials
  reaching nothing. No test touches a network, a real server or a live
  org; transports and connections are mocked at module boundaries.
- Live from a clean folder: `muster demo` → `muster publish` refused (4
  errors) → `--dry-run` printed the plan with the refusal note →
  `--force` published 11 rows to warehouse.db → republish left 11 rows →
  `verify_chain` passed across three manifests → `muster report` still
  found the pipeline run. docs/CONNECTORS.md documents all four targets
  and the security model; README gained a Publishing section; version is
  0.4.0 in both pyproject.toml and the package.

## Deliberately not started

Any UI and daemon/scheduling work — later goals cover them.
