# Goal 5: the dashboard, the scheduler and the hardening pass

Muster now has a local-first web dashboard behind a single login token, a
small cron daemon that runs the pipeline on a schedule, and a hardened
codebase: ruff, mypy --strict, bandit and pip-audit all pass in one `make
lint`, with every dependency pinned by hash.

## What exists

- **`muster serve`** (`web/`): a FastAPI app, server-rendered Jinja2 with
  one hand-written stylesheet in the report's dark instrument-panel
  aesthetic — no CDNs, no build step, no scripts at all. Pages: dashboard
  (latest-run KPIs, per-field quality, trends across runs read from the
  manifests), exceptions browser (filter by severity/kind/file; resolve or
  dismiss with a note — decisions append to `runs/resolutions.jsonl`, an
  audit log keyed by a stable exception fingerprint; the run's own records
  are never rewritten), mapping review (the Goal 3 flow with buttons), a
  run trigger in the header (guarded by a lock, executed in a subprocess),
  and the run report served inline under its own scoped CSP. Binds
  127.0.0.1 by default; any other `--host` prints a warning.
- **Auth** (`web/auth.py`): one token, generated on first serve, stored in
  the OS keyring (service `muster`) with an owner-only `.muster-token`
  file fallback, registered for redaction, compared with
  `secrets.compare_digest`. Sessions are HttpOnly SameSite=Strict cookies
  over an in-memory store with expiry; every mutating form carries a
  per-session CSRF token; login (5/min) and mutations (30/min) are
  rate-limited per client; every response carries a CSP with no script
  sources beyond a per-request nonce, `nosniff`, `no-referrer`, frame
  denial and `no-store`.
- **Scheduling** (`scheduler.py`): a five-field cron parser written
  in-tree (steps, ranges, lists, aliases, the standard
  day-of-month/day-of-week OR rule) — no new dependency. `muster schedule
  "<cron>"` stores the expression beside muster.yaml and shows the next
  three firings; `muster daemon start|stop|status|run` manages a detached
  loop with a PID file and a size-rotated `runs/daemon.log`; each firing
  runs the pipeline in a subprocess, records non-zero exits and notifies
  `MUSTER_WEBHOOK_URL` (validated http(s)) on failure; the schedule file
  is re-read every tick so changes apply without a restart. `muster
  schedule --print` emits a ready-to-use systemd unit and crontab line for
  people who prefer the OS scheduler — the honest recommendation.
- **Hardening**: `make lint` runs ruff (with import-order, bugbear and
  pyupgrade rules), `mypy --strict` over the whole package (33 files,
  zero errors — the one override is scoped to the optional untyped
  psycopg import), bandit (zero findings; every suppression is one line
  with its reason beside it) and pip-audit (clean). Real defects found and
  fixed by the pass: report.py used Python 3.12-only f-string syntax while
  the package claims 3.11 support (would not even parse on 3.11); the
  failure webhook accepted any URL scheme from the environment; the
  Salesforce `instance_url` from the token reply was trusted without
  requiring https before the bearer token was sent to it. `constraints.txt`
  pins every dependency with hashes (`make constraints` regenerates it);
  docs/SECURITY.md states the threat model in plain language, including
  the honest limits (plain HTTP on loopback, tamper-evident-not-proof
  chain, no defence against the user's own account).

## Verification

- 137 tests pass via `pytest`: cron parse/match/next-fire (including the
  dom/dow OR rule and Sunday-as-7), the daemon loop firing once per
  matching minute against a fake clock, webhook notification on failure
  only, daemon start/status/stop against a real detached process, page
  redirects and 401s without a session, wrong-token 401 and login rate
  limiting (429 on the sixth attempt), strict cookie flags, CSRF rejection
  (403) and acceptance, the exceptions resolve flow (append-only log,
  exceptions.csv byte-identical afterwards, latest decision wins), input
  validation on the resolve route (422/404), the browser review flow
  writing the same file `muster review` uses, security headers on pages
  and the scoped report CSP, and the token file being 0600. Web tests
  patch the keyring away and never bind a socket.
- Live: `muster demo` then `muster serve` served the authenticated
  dashboard over the demo on 127.0.0.1 (redirect → login with the
  keyring token → dashboard, CSS, exceptions, report); the daemon, given
  `* * * * *`, fired a real pipeline run on the minute boundary and logged
  it to runs/daemon.log. `make lint` exits 0. Version is 0.5.0 in both
  pyproject.toml and the package.

## Deliberately not started

Packaging and release — Goal 6 covers them.
