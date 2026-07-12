# Connectors: publishing the governed dataset

`muster publish [target]` sends the latest governed dataset — the output of
the most recent `muster run` — to a target configured under `targets:` in
muster.yaml. Four target types exist: **sqlite**, **postgres**, **rest** and
**salesforce**. Every target supports `--dry-run`, which prints exactly what
a publish would do and writes nothing at all, not even a manifest entry.

```sh
muster publish                    # the only configured target
muster publish warehouse          # a named target
muster publish warehouse --dry-run
muster publish warehouse --force  # override the error-run refusal (recorded)
```

Exit codes: `0` when every record landed, `2` when some records failed (see
`publish-exceptions.csv` in the output directory), `1` when the publish
could not proceed at all.

## The security model

Publishing is where Muster first talks to the outside world, so the CIA
triad is applied explicitly:

**Confidentiality — secrets never live in configuration or output.** A
target configuration names *environment variables* (`dsn_env`, `token_env`,
`client_secret_env`, …), never values. Target sections reject unknown keys,
so a credential pasted into muster.yaml fails loudly at load time instead of
sitting on disk. At publish time each secret is resolved from the
environment or, if unset and the optional [keyring](https://pypi.org/project/keyring/)
library is installed, from the OS keyring:

```sh
export MUSTER_PG_DSN='postgresql://muster:...@db.internal/warehouse'
# or, kept out of shell history and process environments entirely:
pip install keyring
keyring set muster MUSTER_PG_DSN
```

Every resolved secret (including access tokens returned by OAuth) is
registered in-process and redacted from all log lines, error messages,
terminal output and manifests before they are written.

**Integrity — the manifest chain covers publishes.** Before anything is
sent, the dataset on disk must hash to exactly what the latest run's
manifest recorded; a stale or tampered file is refused, and `--force` does
not override that. Every publish — successful, partial, failed or forced —
appends its own manifest (`kind: publish`) to the same tamper-evident hash
chain as pipeline runs, recording the target, destination, source run, row
counts, duration and outcome. A `--force` override is written into the
manifest in so many words.

**Availability — retries, timeouts, idempotent writes.** Network targets
retry 429 and 5xx responses and connection failures with exponential
backoff plus jitter, honour `Retry-After` on 429, and every request carries
a timeout. Writes are idempotent so a retried or repeated publish converges
instead of duplicating: relational targets upsert on key columns, and
Salesforce upserts on an External ID field.

A publish is also refused while the latest run holds error-severity
exceptions — an incomplete governed dataset should not quietly propagate
downstream. `--force` overrides that check (loudly); it is meant for
knowingly publishing a partial dataset, not for routine use.

## Key columns

Each target may set `key_columns`; when omitted, the dataset's
`validation.keys` are used. With keys, rows are upserted (matching rows are
updated, new rows inserted). With no keys anywhere, the relational targets
fully refresh the table instead — delete then insert, in one transaction.

## sqlite

Standard library; no extra install. The database file and table are created
if missing, with a `UNIQUE` constraint over the key columns. The publish is
one transaction: all rows land or none do.

```yaml
targets:
  warehouse:
    type: sqlite
    path: warehouse.db        # relative to muster.yaml
    table: receivals
    # key_columns: ["receival_id"]   # defaults to validation.keys
```

If the table already exists *without* a unique constraint on the key
columns, the upsert fails cleanly and the transaction rolls back — create
the constraint or drop the table and let Muster recreate it.

## postgres

Requires psycopg 3: `pip install 'muster[postgres]'`. The connection string
is a secret (it usually embeds a password) and is resolved from `dsn_env`.
Behaviour matches sqlite: table created if missing with a `UNIQUE`
constraint, `INSERT … ON CONFLICT … DO UPDATE`, one transaction — all rows
or none.

```yaml
targets:
  analytics:
    type: postgres
    table: receivals
    dsn_env: MUSTER_PG_DSN    # e.g. postgresql://user:pass@host:5432/db
```

## rest

POSTs the dataset to an endpoint as batches of JSON records, body
`{"records": [...]}`, dates and datetimes as ISO strings. Authentication is
a bearer token (`Authorization: Bearer …`), an API key in a configurable
header, or none — the token always resolved from `token_env`.

```yaml
targets:
  ingest_api:
    type: rest
    url: https://example.com/api/ingest
    auth: bearer              # bearer | api_key | none
    token_env: MUSTER_REST_TOKEN
    # api_key_header: X-API-Key   # used when auth: api_key
    batch_size: 500
    timeout_seconds: 30
    max_retries: 5
```

Transient failures (429, 5xx, network errors) are retried per batch with
exponential backoff and jitter; a `Retry-After` header on 429 is honoured.
A batch that still fails is not silently skipped: its rows — and the rows
of batches not attempted after it — are recorded in
`publish-exceptions.csv`, and the publish exits with code 2.

**Idempotency is the endpoint's half of the contract.** Every record
carries the key columns and a retried batch is resent byte-for-byte, so an
endpoint that upserts on those keys can deduplicate resends safely. If your
endpoint blindly inserts, retries can duplicate — configure it to upsert.

## salesforce

Upserts records into a Salesforce object through the REST API using an
**External ID field**. Both the field mapping and the External ID are user
configuration: Muster cannot know your org's schema, so only the fields you
map are sent, and `field_map` must map some canonical field onto the
External ID field.

```yaml
targets:
  crm:
    type: salesforce
    object: Receival__c              # any sObject, standard or custom
    external_id_field: Ticket_Id__c  # must be an External ID field in the org
    field_map:                       # canonical field -> Salesforce API name
      receival_id: Ticket_Id__c
      grower: Grower_Name__c
      tonnes: Net_Tonnes__c
    auth_flow: client_credentials    # or username_password
    login_url: https://login.salesforce.com   # or your My Domain / sandbox URL
    api_version: v62.0
    batch_size: 200                  # Salesforce's ceiling per request
```

Authentication is OAuth2 with credentials strictly from the environment or
keyring:

- `client_credentials` (recommended): `MUSTER_SF_CLIENT_ID`,
  `MUSTER_SF_CLIENT_SECRET` — a connected app with the client-credentials
  flow enabled and a run-as user.
- `username_password`: additionally `MUSTER_SF_USERNAME` and
  `MUSTER_SF_PASSWORD` (the password with the security token appended, as
  Salesforce requires).

The environment variable names themselves are configurable
(`client_id_env:` etc.) if you run several orgs side by side.

Records go through the sObject Collections endpoint
(`PATCH /composite/sobjects/{object}/{externalIdField}`) in batches of up
to 200 with `allOrNone: false`: Salesforce reports each record separately,
and every failure lands in `publish-exceptions.csv` with its Salesforce
error code (`REQUIRED_FIELD_MISSING`, `DUPLICATE_VALUE`, …) and the
record's key. Rows with an empty External ID value are recorded and never
sent. Upserting on an External ID is idempotent: republishing the same
dataset updates rather than duplicates.

All Salesforce behaviour is integration-tested against a mock server; the
test suite never contacts a live org.

## The audit trail

Every publish appends a manifest to `runs/`:

```json
{
  "kind": "publish",
  "publish": {
    "target": "warehouse",
    "type": "sqlite",
    "destination": "sqlite database …/warehouse.db, table receivals",
    "source_run": "20260712T050825Z",
    "rows": 11,
    "rows_sent": 11,
    "rows_failed": 0,
    "outcome": "published",
    "forced": false
  },
  "previous_manifest": { "run_id": "…", "sha256": "…" }
}
```

`outcome` is `published`, `partial` or `failed`; failed publishes are
recorded too. The chain hashes verify end to end across runs and publishes
alike, so where the dataset went — and under what conditions — can be
checked rather than trusted.
