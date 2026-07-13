# Changelog

All notable changes to Muster are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Muster
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] — 2026-07-13

Web interface release: every existing operating capability now has a calm,
coherent place in the local dashboard, with the complete demo ready for a
live walkthrough.

### Added

- Dedicated read-only trends, remediation and publishing views. Trends show
  quality, published and held counts across run manifests; publishing shows
  configured targets, the latest publish outcome, recorded quality overrides,
  and schedule/daemon status without resolving or exposing credentials.
- A full report view inside the shared dashboard shell, with the standalone
  self-contained report still available for sharing.
- Explicit fresh-project, clean-run, in-progress, failed-action and malformed-
  artefact states, plus route/render coverage for populated and empty pages.
- A guided demo click-path in `docs/WORKFLOW.md`, including the held-row
  correction and rerun arc.

### Changed

- Dashboard, exceptions, remediation, mapping review, trends, publishing and
  report now share one active, keyboard-operable navigation header. Inputs are
  labelled, focus is visible, statuses pair colour with text, and the strict
  zero-JavaScript CSP remains unchanged.
- `muster demo` now records a synthetic local SQLite publish, a weekday
  schedule and an illustrative mapping-review fixture so `muster serve` opens
  a complete interface immediately.

## [1.2.0] — 2026-07-13

Exception remediation: corrected rows rejoin the governed dataset under
audit. The human loop on exceptions is now closed — a held row is a work
item, not a dead end.

### Added

- Corrections in the audit log: `runs/resolutions.jsonl` records gain an
  optional `corrected_values` map (canonical field → new value) alongside
  the existing note and decision. History is never mutated — a
  re-correction is a newer record superseding the old, and a later resolve
  or dismiss withdraws the correction. Pre-1.2 records remain readable.
- A remediation stage in `muster run`: held rows whose exception
  fingerprints have recorded corrections get the corrected values applied
  and the whole row re-validated against the full rule set — a correction
  that still fails the rules leaves the row held, with a new exception
  saying why. Recovered rows appear in `exceptions.csv` as a `remediated`
  warning, and the manifest records how many rows entered via remediation
  and the resolution ids used.
- An inline correction form in the exceptions browser (current value,
  proposed value, required note) with server-side coercion and rule checks
  that name the precise failure; a "remediated — awaiting rerun" filter;
  and a dashboard tile counting rows corrected but not yet rerun.
- `muster resolve <fingerprint> --set field=value --note "..."` for
  headless corrections with identical validation, and a
  "N row(s) recovered via remediation" line in the run summary.
- The demo now walks the full arc: it prints the exact `muster resolve`
  command that corrects the uncoercible weight on ticket R-2004, and a
  rerun watches held drop 5 → 4 and published rise 11 → 12.
- `docs/WORKFLOW.md` gains "Fixing what was held": both correction paths,
  the re-validation rule, and the supersede-by-new-record audit model.

### Changed

- Exception fingerprints are now stable across runs: the digest covers the
  defect itself (file, row, column, value, kind, reason) and no longer
  includes the run id, so a recorded decision follows the defect from run
  to run. Resolutions recorded before 1.2.0 keep their history in the log
  but no longer match — re-record any still-relevant decision from the
  exceptions browser.

## [1.1.0] — 2026-07-13

Deployment release: everything an organisation needs to run Muster in a
container platform. No changes to the pipeline itself.

### Added

- A multi-stage `Dockerfile` (python:3.12-slim): the build stage compiles
  the wheel, the runtime stage installs it with the postgres extra and
  runs as non-root uid 10001, with a stdlib-only `HEALTHCHECK` against the
  new liveness probe. Plus `.dockerignore`.
- `deploy/docker-compose.yaml`: the daemon on a schedule over a mounted
  project, publishing to PostgreSQL, secrets from a gitignored env file —
  every decision explained in comments. `deploy/example.env.template`
  lists every variable; the real env file is gitignored.
- Operational probes on `muster serve`: `GET /healthz` (liveness) and
  `GET /readyz` (readiness — configuration parses, runs directory
  writable; 503 with a terse reason otherwise). Deliberately
  unauthenticated, disclosing nothing beyond a status word; security
  headers still apply.
- `MUSTER_LOG_FORMAT=json`: one JSON object per log line (`ts`, `level`,
  `logger`, `msg`) for container log drivers and SIEMs. Default logfmt
  output unchanged; secret redaction covers both formats, tracebacks
  included.
- `docs/DEPLOYMENT.md`: the adoption playbook — the parallel-run pilot
  pattern, container and compose usage, secrets guidance, scheduling
  options, and where the manifest chain fits a governance conversation.

## [1.0.1] — 2026-07-13

### Fixed

- The CI lint job failed in `mypy --strict` with "Library stubs not
  installed for yaml" in config.py, scaffold.py and assist.py. The dev
  extra never declared `types-PyYAML`; the check passed locally only
  because the stubs happened to be installed there. The stubs are now a
  dev dependency, so the local and CI environments are identical.
- The lint job upgrades pip before installing: `pip-audit` audits the
  whole environment, pip included, so a stale runner pip with known
  vulnerabilities would fail the gate on its own.

## [1.0.0] — 2026-07-13

First stable release. No behavioural changes to the pipeline; this release
finishes the documentation, packaging and release engineering.

### Added

- `docs/CONFIG.md` — every configuration key, annotated — and
  `docs/WORKFLOW.md`, the workflow as a narrative from `profile` to
  `schedule`.
- A GitHub Actions CI workflow: lint gate (ruff, mypy --strict, bandit,
  pip-audit), tests on Python 3.11 and 3.12, and a build job with
  `twine check`.
- `py.typed` marker: the package ships its type information.
- Project URLs, final classifiers and this changelog.

### Changed

- README rewritten around the problem, the philosophy and the honest
  limitations, with a features table, an architecture diagram and a report
  screenshot (`docs/report.png`).

## [0.5.0] — 2026-07-12

### Added

- `muster serve`: a local-first web dashboard — latest run, per-field
  quality, trends across runs, an exceptions browser (resolve or dismiss
  with a note; decisions append to `runs/resolutions.jsonl`, never
  rewriting run records), the mapping review flow, a run trigger, and the
  run report inline. Server-rendered, one hand-written stylesheet, no
  CDNs, no build step, no scripts.
- Authentication for the dashboard: a single login token generated on
  first serve and stored in the OS keyring (owner-only file fallback),
  HttpOnly SameSite=Strict sessions, per-session CSRF tokens on every
  form, rate-limited login and mutating routes, and strict security
  headers (CSP with a nonce as the only script source, nosniff,
  no-referrer, frame denial).
- Scheduling: `muster schedule "<cron>"` with an in-tree five-field cron
  parser (steps, ranges, lists, aliases, the standard day-of-month/
  day-of-week OR rule), `muster daemon start|stop|status|run` with a PID
  file and size-rotated `runs/daemon.log`, failure notification to
  `MUSTER_WEBHOOK_URL`, and `muster schedule --print` emitting a systemd
  unit and crontab line.
- `make lint` as the hardening gate: ruff, `mypy --strict` over the whole
  package, bandit and pip-audit, plus `constraints.txt` pinning every
  dependency with hashes.
- `docs/SECURITY.md`: the threat model stated plainly, including limits.

### Fixed

- `report.py` used Python 3.12-only f-string syntax while the package
  claims 3.11 support; the module failed to parse on 3.11.

### Security

- The failure webhook now refuses non-http(s) URL schemes from the
  environment.
- The Salesforce `instance_url` returned by the token endpoint must be
  https before the bearer token is sent to it.

## [0.4.0] — 2026-07-12

### Added

- `muster publish [target]`: publish the latest governed dataset to
  targets configured in muster.yaml — sqlite (standard library), postgres
  (psycopg 3, optional extra), generic REST (batched JSON, bearer/API-key
  auth, exponential backoff with jitter, `Retry-After` honoured) and
  Salesforce (sObject Collections upsert on a configured External ID
  field, OAuth2 client-credentials or username-password, per-record
  failures recorded with Salesforce error codes).
- Secret handling (`credentials.py`): target configurations name
  environment variables, never values; secrets resolve from the
  environment or the OS keyring; every resolved secret is redacted from
  logs, errors, terminal output and manifests. Target sections reject
  unknown keys so a pasted credential fails at load time.
- Publish integrity: the dataset must hash to what the latest run's
  manifest recorded (`--force` does not override); a run with
  error-severity exceptions is refused unless `--force`, which is recorded
  loudly; every publish appends a `kind: publish` manifest to the
  tamper-evident chain. `--dry-run` prints the plan and writes nothing.
- Per-record publish failures land in `publish-exceptions.csv` with exit
  code 2. `docs/CONNECTORS.md` documents all four targets.

## [0.3.0] — 2026-07-12

### Added

- `muster init --from <folder>`: propose a configuration from real files —
  heading variants clustered, types inferred, synonyms kept — with every
  inference marked `PROPOSED` and refused until reviewed; `muster confirm`
  accepts the remainder.
- Opt-in LLM-assisted mapping (`muster run --assist` + `muster review`):
  proposals for columns fuzzy matching cannot place, applied only after
  human acceptance. Requires `MUSTER_LLM_API_KEY`; sends only column
  headings, inferred types and at most five redacted sample values — no
  cell data, no file names — and records exactly what was sent.
- `muster demo`: a synthetic grain-receivals demo (three deliberately
  disagreeing files, entirely invented values) run end to end.
- `scripts/bench.py` and `docs/PERFORMANCE.md`: 5 million rows in under
  nine seconds at ~2 GiB peak on a laptop, with the method and caveats.

### Changed

- Coercion and validation vectorised as Polars expressions; sources read
  in bounded chunks; reconciliation only partitions duplicated keys.

## [0.2.0] — 2026-07-11

### Added

- Validation engine: per-field rules (range, regex, allowed_values) and
  structured cross-field comparisons, at error or warning severity —
  errors hold rows out of the governed dataset.
- Reconciliation of duplicate keys across files: lossless merge when rows
  agree, conflict exceptions when they do not, optional survivorship
  (`newest_file`, `priority_list`, `manual`) — never a silent guess.
- Self-contained HTML run report: completeness, validity, per-file quality
  scores, mapping decisions, exceptions, held conflicts.
- Tamper-evident run manifests: SHA-256 of configuration, inputs and
  outputs, each manifest chained to its predecessor.

## [0.1.0] — 2026-07-11

### Added

- The consolidation pipeline: read CSV/XLSX with safe readers, map columns
  (exact, synonyms, fuzzy with a threshold), coerce to declared types with
  per-cell failure capture, and write Parquet + CSV plus `exceptions.csv` —
  nothing guessed silently, nothing dropped without a written exception.
- `muster init`, `muster profile` and `muster run`; Pydantic-validated
  `muster.yaml`; path confinement, file size limits and structured
  logging.

[1.3.0]: https://github.com/pcguest/muster/releases/tag/v1.3.0
[1.2.0]: https://github.com/pcguest/muster/releases/tag/v1.2.0
[1.1.0]: https://github.com/pcguest/muster/releases/tag/v1.1.0
[1.0.1]: https://github.com/pcguest/muster/releases/tag/v1.0.1
[1.0.0]: https://github.com/pcguest/muster/releases/tag/v1.0.0
