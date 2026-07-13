# Deploying Muster

How to take Muster from a laptop experiment to something an organisation
relies on. None of this changes what Muster does — it is the same pipeline
whether it runs from a terminal, a container or a scheduler; this document
is about running it where the stakes are higher.

## Start with a parallel run

Do not cut anything over on day one. The pattern that works:

1. **Pick one process** — one folder of spreadsheets, one consolidated
   output that someone currently produces by hand.
2. **Run Muster beside the manual process**, on the same input files, for
   a few cycles. The manual result remains the one people use.
3. **Reconcile daily.** Compare Muster's governed dataset against the
   manual output. Every difference is one of three things: a Muster
   mapping or rule that needs adjusting, a defect in the manual process
   that nobody had noticed, or a genuine ambiguity in the source data.
   All three are worth finding, and `exceptions.csv` usually names the
   row and file before you have to hunt for it.
4. **Cut over only when the reconciliation is boring** — when differences
   have stopped appearing, or every remaining one is understood and
   accepted in writing. Keep the manual process available for one more
   cycle after cut-over.

The exceptions file makes this pilot honest: Muster does not hide the
cases where the spreadsheets disagreed, it lists them. Expect the first
runs to produce many exceptions — that is the dirt in the data becoming
visible, not a tool failure. Triage them in the dashboard, tighten the
configuration, and watch the count fall run over run.

## The container image

The repository ships a multi-stage `Dockerfile`: the build stage compiles
the wheel, the runtime stage installs it with the postgres extra and
nothing else, and the process runs as uid 10001, non-root, with no
packages added to the slim base. The entrypoint is the `muster` CLI:

```sh
docker build -t muster:1.1.0 .
docker run --rm -v "$PWD/project:/project" -w /project muster:1.1.0 run
```

The mounted `/project` directory is the whole deployment state:
`muster.yaml`, `muster.schedule`, `sources/`, `output/` and the `runs/`
audit chain. It must be writable by uid 10001, and it is the thing to
back up.

[`deploy/docker-compose.yaml`](../deploy/docker-compose.yaml) shows the
realistic shape — the daemon on a schedule, a PostgreSQL publish target,
secrets from a gitignored env file — with each decision explained in a
comment. Copy [`deploy/example.env.template`](../deploy/example.env.template)
to `deploy/muster.env`, fill in real values and never commit the copy.

## Operational surface

`muster serve` answers two unauthenticated probes for orchestrators:

- `GET /healthz` — liveness: the process is up. Always `{"status": "ok"}`.
- `GET /readyz` — readiness: the configuration parses and the runs
  directory is writable. On failure it returns 503 with a terse reason
  and no detail an unauthenticated caller could use.

The image's `HEALTHCHECK` probes `/healthz`; workloads running the daemon
instead of the dashboard should override it, as the compose example does.

Set `MUSTER_LOG_FORMAT=json` and every log line becomes one JSON object
(`ts`, `level`, `logger`, `msg`) that log drivers and SIEMs ingest without
parsing rules. The default logfmt output is unchanged, and secret
redaction applies to both formats, tracebacks included.

## Secrets

The rules do not bend for convenience:

- **Secrets never live in muster.yaml.** Target sections name environment
  variables (`dsn_env: MUSTER_PG_DSN`); the value is resolved at publish
  time and redacted from logs, errors, output and manifests.
- **In containers and CI, inject through the environment** — an orchestrator
  secret store, a vault agent, or compose's `env_file` pointing at a file
  that is gitignored and permissioned to the operator.
- **On workstations, use the OS keyring**
  (`keyring set muster MUSTER_PG_DSN`) rather than a shell profile export
  that every process can read.
- If a real secret ever lands in a file that reaches version control,
  rotate it. Deleting the commit is not enough.

## Scheduling

Three options, in order of preference where each is available:

- **systemd or cron on the host**: `muster schedule --print` emits a unit
  file and a crontab line. The host's scheduler is supervised, logged and
  familiar to whoever operates the machine.
- **An orchestrator's scheduler** (Kubernetes CronJob, ECS scheduled task,
  compose + the daemon): run the image with `run` as the command on the
  platform's timer, or keep the container alive with `daemon run` as the
  compose example does. Exit codes are honest — 0 clean, 2 completed with
  error-severity exceptions, 1 failed — so the platform's failure signal
  means something.
- **The in-tree daemon** (`muster daemon start`) where neither exists.
  It is a convenience, not a supervisor: one-minute granularity, a PID
  file and a rotated log, nothing more.

Failed scheduled runs notify the webhook named by `MUSTER_WEBHOOK_URL`
(http and https only), whichever scheduler drives them.

## The audit conversation

When governance, audit or risk asks "how do you know the published
numbers are what the pipeline produced?", the manifest chain is the
answer. Every run and every publish appends a manifest recording SHA-256
digests of the configuration, the input files and the outputs, chained to
the manifest before it. Practical implications worth stating in that
conversation:

- Lineage is checkable: a published dataset hashes to what the recorded
  run produced, and `muster publish` refuses to send anything that does
  not (`--force` overrides are themselves recorded, loudly).
- Human decisions are appended, never rewritten: exception resolutions
  land in `runs/resolutions.jsonl` with who-did-what preserved.
- The chain is tamper-evident, not tamper-proof: anyone with write access
  to `runs/` could rewrite it consistently. Copy manifests somewhere the
  pipeline's credentials cannot write — object storage with retention,
  a log shipper — and the copy anchors the chain.

## What this is not

Muster is a single-node tool for spreadsheet-scale data. It is not a
warehouse, not a multi-user platform, and its dashboard is deliberately
single-user on loopback. The consolidated dataset is held in memory
during a run, and the dashboard's remote answer is an SSH tunnel, not a
public bind. The README's
[Limitations, honestly](../README.md#limitations-honestly) section is the
full list; read it before promising anyone a data platform.
