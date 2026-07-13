# Security model

This page states Muster's threat model in plain language: what is trusted,
what is not, and what protects each boundary. It is written to be read, not
to reassure — where a protection has limits, the limits are stated.

## The one-sentence version

Muster treats every spreadsheet as hostile, every model reply and API
response as hostile, keeps every secret out of configuration and output,
records everything it does in a tamper-evident chain, and talks to the
network only when you configure it to — with the web dashboard bound to
your own machine unless you explicitly say otherwise.

## Untrusted inputs

Source files are the main attack surface: they come from other people's
machines and spreadsheets, and Muster's whole job is to read them.

- Files are parsed with safe readers only — no formula evaluation, no
  pickle, no `eval`, YAML via `safe_load`.
- Discovery is confined to the configured root: resolved paths (including
  symlink targets) must stay inside it, hidden directories and the output
  and runs directories are skipped, and a configurable size limit rejects
  files large enough to exhaust memory. Oversized or escaping files become
  exception records, not reads.
- A crafted *value* cannot become code or markup anywhere downstream:
  every value in the HTML report is escaped; every string in a generated
  configuration is JSON-quoted so a heading cannot inject YAML structure;
  cross-field rules are structured comparisons, never evaluated
  expressions.
- Parser crashes are treated as security bugs, because untrusted input
  must not be able to stop the tool: the one known Polars panic (vectorised
  month-name date parsing) is structurally avoided and pinned by a
  regression test.
- Model replies (`--assist`) and API responses (publish targets) are
  untrusted too: parsed defensively, validated against known columns and
  fields, dropped when malformed. An assist proposal is never applied
  without a human accepting it.

## Local-first

Muster is a local tool. Nothing leaves the machine unless you configure a
publish target, set a webhook, or opt into `--assist` (which sends only
column headings, inferred types and redacted samples — never cell data,
never file names; docs/CONNECTORS.md and the README state this in full).

The web dashboard (`muster serve`) binds 127.0.0.1 by default; binding any
other address requires an explicit `--host` and prints a warning. It is
single-user: one login token, generated on first serve and stored in the
OS keyring (or an owner-only file when no keyring backend exists), required
for every page. Sessions are HttpOnly, SameSite=Strict cookies over an
in-memory store; every mutating form carries a per-session CSRF token;
mutating routes and login attempts are rate-limited; and every response
carries a strict Content-Security-Policy (no script sources beyond a
per-request nonce — the pages ship no scripts at all), `nosniff`,
`Referrer-Policy: no-referrer` and frame denial.

Two routes are deliberately unauthenticated: `GET /healthz` (liveness) and
`GET /readyz` (readiness), for orchestrator probes that cannot hold a
session. Each returns a status word and, on 503, a terse reason — no
paths, no configuration detail, nothing an unauthenticated caller can use
to map the deployment. Both still carry the security headers.

Honest limits: the dashboard speaks plain HTTP, which is fine on loopback
but not across a network — if you must reach it remotely, use an SSH
tunnel rather than `--host 0.0.0.0`. The cookie cannot be marked `Secure`
for that reason. Anyone with the same OS user account can read the keyring
entry or token file; Muster does not defend against your own account being
compromised.

## Secrets

Secrets never live in muster.yaml, logs, manifests or terminal output.

- Target configurations name *environment variables*; the values are
  resolved at publish time from the environment or the OS keyring
  (`keyring set muster <NAME>`). Target sections reject unknown keys, so a
  pasted credential fails loudly at load time, and `*_env` fields must
  look like environment variable names.
- Every resolved secret — including OAuth access tokens that only exist
  mid-publish — is registered in-process and redacted from log lines,
  error messages, terminal output and manifests, longest first, so a
  password embedded in a connection string cannot leave a fragment behind.
- The assist API key comes only from `MUSTER_LLM_API_KEY`; the web login
  token lives in the keyring or an owner-only file.

## Integrity

Every pipeline run and every publish appends a manifest to
`runs/<timestamp>/manifest.json`, recording SHA-256 digests of the
configuration, all inputs and all outputs, plus row counts, duration and
outcome. Each manifest embeds the SHA-256 of its predecessor, forming a
hash chain: altering any historic manifest breaks verification of every
manifest after it. Publishing verifies the dataset on disk hashes to
exactly what the latest run recorded before anything is sent (`--force`
does not override this), a run with error-severity exceptions is refused
without `--force`, and a forced publish is written into the chain in so
many words. Exception decisions made in the dashboard append to
`runs/resolutions.jsonl` — an audit log; the run's own records are never
rewritten.

Honest limit: the chain is tamper-*evident*, not tamper-*proof*. An
attacker with write access to the runs directory could rewrite the entire
chain consistently; defending against that requires copying manifests
somewhere they cannot write, which is out of Muster's hands.

## Availability

Network operations carry timeouts everywhere. Publishing retries 429 and
5xx responses and connection failures with exponential backoff and jitter,
honours `Retry-After`, and writes idempotently (upserts on key columns or
an External ID) so a retried publish converges instead of duplicating.
Relational publishes are single transactions: all rows or none. The
scheduling daemon runs each pipeline in a subprocess so a crashing run
cannot take the daemon down, records non-zero exits, and can notify a
webhook (URL from `MUSTER_WEBHOOK_URL`, http(s) only).

## Static gates

`make lint` must pass before a change lands: ruff, `mypy --strict` over
the whole package, bandit (every suppression is a single line with the
reason beside it — no blanket ignores), and `pip-audit` over the
environment. `constraints.txt` pins every dependency with hashes;
`pip install -r constraints.txt --require-hashes` reproduces a verified
environment.

## Reporting

If you find a security problem, open an issue marked *security* — or, if
it should not be public yet, email the author directly (address in
pyproject.toml).
