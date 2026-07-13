"""Command-line interface: init, confirm, profile, run, demo, review,
resolve, publish, report, schedule, daemon and serve."""

import dataclasses
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from muster import __version__
from muster.assist import (
    REVIEW_FILE_NAME,
    AssistError,
    AssistUnavailable,
    accepted_synonyms,
    load_review_file,
    propose_mappings,
    write_review_file,
)
from muster.config import CONFIG_TEMPLATE, Config, ConfigError, load_config
from muster.credentials import redact_text
from muster.demo import write_demo
from muster.logs import configure_logging
from muster.manifest import RUNS_DIRECTORY, latest_manifest_of_kind
from muster.pipeline import PipelineError, RunResult, run_pipeline
from muster.profiling import FileProfile, profile_folder
from muster.publish import PublishError, publish_dataset
from muster.remediation import (
    MAX_NOTE_LENGTH,
    append_resolution,
    check_correction,
    load_exceptions,
)
from muster.report import REPORT_DATA_NAME, RunReportData, write_report
from muster.scaffold import confirm_text, propose_config
from muster.scheduler import (
    CronExpression,
    SchedulerError,
    configure_daemon_logging,
    cron_line,
    daemon_loop,
    daemon_pid,
    read_schedule,
    start_daemon,
    stop_daemon,
    systemd_unit,
    write_schedule,
)

logger = logging.getLogger(__name__)

app = typer.Typer(
    help="Consolidate inconsistent spreadsheets into one governed dataset.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()
errors = Console(stderr=True, style="bold red")


@app.callback()
def main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Show detailed logging.")
    ] = False,
) -> None:
    configure_logging(verbose)
    logger.debug("muster version=%s", __version__)


@app.command()
def init(
    path: Annotated[
        Path, typer.Option(help="Where to write the starter configuration.")
    ] = Path("muster.yaml"),
    from_folder: Annotated[
        Path | None,
        typer.Option(
            "--from",
            help="Propose the canonical schema by profiling this folder's files.",
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing file.")
    ] = False,
) -> None:
    """Write a starter muster.yaml, or propose one from real files.

    With --from, the schema is inferred by profiling the folder and every
    inference is marked PROPOSED: muster refuses to run until you review
    them and remove the markers (or accept them all with 'muster confirm').
    """
    if path.exists() and not force:
        errors.print(f"{path} already exists; pass --force to overwrite it.")
        raise typer.Exit(code=1)
    if from_folder is None:
        path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        console.print(
            f"Wrote {path}. Edit the canonical schema and sources, then run 'muster run'."
        )
        return
    try:
        profiles = profile_folder(from_folder)
        text = propose_config(profiles, from_folder.as_posix())
    except (FileNotFoundError, ConfigError) as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    path.write_text(text, encoding="utf-8")
    fields = text.count("  - name:")
    console.print(
        f"Proposed {path} with {fields} field(s) from "
        f"{sum(1 for p in profiles if p.error is None)} file(s)."
    )
    console.print(
        "Every inference is marked PROPOSED and muster will not run until you "
        "review them: edit the file, or accept them all with 'muster confirm'."
    )


@app.command()
def confirm(
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
) -> None:
    """Accept every PROPOSED inference in a generated configuration."""
    if not config_path.is_file():
        errors.print(f"configuration file not found: {config_path}")
        raise typer.Exit(code=1)
    text = config_path.read_text(encoding="utf-8")
    try:
        confirmed, count = confirm_text(text)
    except ConfigError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    if not count:
        console.print(f"{config_path} has no PROPOSED markers; nothing to confirm.")
        return
    config_path.write_text(confirmed, encoding="utf-8")
    console.print(
        f"Confirmed {count} proposed inference(s); {config_path} is now the "
        "configuration of record."
    )


def _render_profile(profile: FileProfile) -> None:
    if profile.error is not None:
        errors.print(f"{profile.file}: {profile.error}")
        return
    table = Table(title=f"{profile.file} — {profile.rows} row(s)", title_justify="left")
    table.add_column("Column")
    table.add_column("Inferred type")
    table.add_column("Non-empty", justify="right")
    table.add_column("Issues")
    for column in profile.columns:
        table.add_row(
            column.name,
            column.inferred_type,
            str(column.non_empty),
            "; ".join(column.issues) or "—",
        )
    console.print(table)


@app.command()
def profile(
    folder: Annotated[
        Path, typer.Argument(help="Folder of .csv/.xlsx files to inspect.")
    ],
    output: Annotated[
        Path, typer.Option(help="Where to write the JSON report.")
    ] = Path("profile.json"),
    max_file_size_mb: Annotated[
        int, typer.Option(help="Skip files larger than this.", min=1)
    ] = 100,
) -> None:
    """Report columns, inferred types, row counts and format inconsistencies."""
    try:
        profiles = profile_folder(folder, max_file_size_mb)
    except FileNotFoundError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    if not profiles:
        errors.print(f"no .csv or .xlsx files found in {folder}")
        raise typer.Exit(code=1)
    for file_profile in profiles:
        _render_profile(file_profile)
    report = {"files": [dataclasses.asdict(p) for p in profiles]}
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    console.print(f"Profiled {len(profiles)} file(s); report written to {output}.")


def _print_run_summary(result: RunResult) -> None:
    console.print(
        f"Published {result.rows_published} of {result.rows_in} row(s) from "
        f"{result.files_read} file(s); {result.rows_held} held, "
        f"{result.rows_superseded} superseded."
    )
    if result.rows_remediated:
        console.print(
            f"  {result.rows_remediated} row(s) recovered via remediation "
            f"(resolutions {', '.join(result.remediation_resolutions)})."
        )
    console.print(f"  dataset:    {result.output_parquet} and {result.output_csv}")
    console.print(
        f"  exceptions: {result.error_count} error(s), {result.warning_count} "
        f"warning(s) in {result.exceptions_csv}"
    )
    console.print(f"  report:     {result.report_html}")
    console.print(f"  manifest:   {result.manifest_path}")


def _propose_and_write(
    config: Config, root: Path, unmapped_samples: dict[str, list[str]]
) -> None:
    """The --assist tail of a run: propose, record, never apply."""
    if not unmapped_samples:
        console.print("Assist: every column mapped; nothing to propose.")
        return
    try:
        review = propose_mappings(unmapped_samples, config)
    except AssistUnavailable as exc:
        console.print(f"Assist: {exc}")
        return
    except AssistError as exc:
        errors.print(f"Assist failed: {exc}")
        return
    review_path = root / REVIEW_FILE_NAME
    write_review_file(review, review_path)
    console.print(
        f"Assist: sent column headings, inferred types and up to "
        f"{config.assist.max_samples} redacted sample value(s) per column to "
        f"{config.assist.provider} model '{config.assist.model}' — no cell "
        "data, no file names."
    )
    console.print(
        f"  {len(review.proposals)} proposal(s) written to {review_path}. Nothing "
        "is applied until you accept with 'muster review' (or edit the file)."
    )


@app.command()
def run(
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
    assist: Annotated[
        bool,
        typer.Option(
            "--assist",
            help="Propose mappings for unmapped columns with the configured LLM "
            "(needs MUSTER_LLM_API_KEY; proposals require human review).",
        ),
    ] = False,
) -> None:
    """Consolidate, validate and reconcile all configured sources.

    Exits 0 on a clean run (warnings allowed), 2 if any error-severity
    exceptions were recorded, and 1 if the run could not proceed — so a CI
    job or cron entry fails loudly when the governed dataset is incomplete.
    """
    try:
        config = load_config(config_path)
        root = config_path.resolve().parent
        accepted = accepted_synonyms(root / REVIEW_FILE_NAME, config)
        result = run_pipeline(config, root, config_path.resolve(), accepted)
    except (ConfigError, PipelineError, AssistError) as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    _print_run_summary(result)
    if assist:
        _propose_and_write(config, root, result.unmapped_samples)
    if result.error_count:
        raise typer.Exit(code=2)


@app.command()
def demo(
    path: Annotated[
        Path, typer.Option(help="Where to create the demo project.")
    ] = Path("demo"),
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing demo folder's files.")
    ] = False,
) -> None:
    """Generate the synthetic grain-receivals demo and run the pipeline on it.

    Three invented sites record the same receivals with clashing headings,
    date formats and conventions, plus deliberate conflicts and rule
    violations. Every value is invented; no real growers, sites or
    organisations appear.
    """
    if path.exists() and not force:
        errors.print(f"{path} already exists; pass --force to write the demo anyway.")
        raise typer.Exit(code=1)
    if force and path.exists():
        shutil.rmtree(path)
    files = write_demo(path)
    console.print(f"Wrote the demo project to {path}/:")
    for written in files:
        console.print(f"  {written}")
    config_path = (path / "muster.yaml").resolve()
    try:
        config = load_config(config_path)
        result = run_pipeline(config, config_path.parent, config_path)
    except (ConfigError, PipelineError) as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    console.print()
    _print_run_summary(result)
    console.print(
        f"\nThe demo data misbehaves on purpose: {result.error_count} error(s) "
        f"and {result.warning_count} warning(s) are expected, and a real "
        "'muster run' would exit with code 2 here."
    )
    console.print(
        f"Open {result.report_html} to see the run report, and try "
        f"'muster init --from {path / 'sources'}' to watch a configuration "
        "being proposed from these files."
    )
    root = config_path.parent
    held_weight = next(
        (
            row
            for row in load_exceptions(root, config)
            if row.kind == "coercion" and row.value == "n/a"
        ),
        None,
    )
    if held_weight is not None:
        console.print(
            "\nOne held row is fixable from here: ticket R-2004's weight "
            "reads 'n/a', so the row cannot join the governed dataset. "
            "Suppose the weighbridge docket shows 27.9 tonnes — record the "
            f"correction and rerun (from {path}/):"
        )
        console.print(
            f"  muster resolve {held_weight.id} --set tonnes=27.9 "
            '--note "weighbridge docket shows 27.9 t"'
        )
        console.print("  muster run")
        console.print(
            "Held drops 5 → 4 and published rises 11 → 12; the new run's "
            "manifest records that the row entered via your recorded decision."
        )
    console.print(
        f"\nA 'warehouse' sqlite target is configured too: from {path}/, try "
        "'muster publish warehouse --dry-run'. The demo also performs one "
        "forced publish to its local SQLite target so the dashboard can show "
        "the complete audited outcome."
    )
    try:
        published = publish_dataset(config, root, "warehouse", force=True)
    except PublishError as exc:
        errors.print(f"Could not populate the demo publish history: {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"Published {published.rows_sent} synthetic row(s) to warehouse.db; "
        f"the forced quality override is recorded in {published.manifest_path}."
    )
    console.print(
        f"From {path}/, run 'muster serve' for the complete dashboard. The "
        "bundled mapping proposal and weekday schedule are ready to review."
    )


@app.command()
def review(
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
    review_file: Annotated[
        Path | None,
        typer.Option("--file", help="Review file; defaults to mapping-review.yaml."),
    ] = None,
    accept_all: Annotated[
        bool, typer.Option("--accept-all", help="Accept every pending proposal.")
    ] = False,
    reject_all: Annotated[
        bool, typer.Option("--reject-all", help="Reject every pending proposal.")
    ] = False,
) -> None:
    """Accept or reject assisted-mapping proposals, one by one.

    Accepted proposals are honoured by every later run; rejected ones are
    kept in the file as a record but never applied.
    """
    if accept_all and reject_all:
        errors.print("--accept-all and --reject-all cannot be combined.")
        raise typer.Exit(code=1)
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    path = review_file or config_path.resolve().parent / REVIEW_FILE_NAME
    if not path.is_file():
        errors.print(f"no review file at {path}; run 'muster run --assist' first.")
        raise typer.Exit(code=1)
    try:
        review_doc = load_review_file(path)
    except AssistError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc

    known = {spec.name for spec in config.fields}
    pending = [p for p in review_doc.proposals if p.status == "pending"]
    if not pending:
        console.print(f"No pending proposals in {path}.")
        return

    accepted = rejected = 0
    for proposal in pending:
        if proposal.target is None or proposal.target not in known:
            proposal.status = "rejected"
            rejected += 1
            console.print(
                f"'{proposal.column}': no usable target proposed — rejected."
            )
            continue
        if accept_all:
            proposal.status = "accepted"
            accepted += 1
            continue
        if reject_all:
            proposal.status = "rejected"
            rejected += 1
            continue
        console.print(
            f"\n'{proposal.column}' → {proposal.target}  "
            f"(confidence {proposal.confidence:.0f})"
        )
        if proposal.rationale:
            console.print(f"  rationale: {proposal.rationale}")
        if proposal.samples:
            console.print(f"  samples sent (redacted): {', '.join(proposal.samples)}")
        if typer.confirm("Accept this mapping?"):
            proposal.status = "accepted"
            accepted += 1
        else:
            proposal.status = "rejected"
            rejected += 1

    write_review_file(review_doc, path)
    console.print(
        f"\nRecorded {accepted} accepted and {rejected} rejected proposal(s) in "
        f"{path}. Accepted mappings apply from the next 'muster run'."
    )


@app.command()
def resolve(
    fingerprint: Annotated[
        str,
        typer.Argument(
            help="The exception's fingerprint, shown in the dashboard's "
            "exceptions browser (16 hex characters)."
        ),
    ],
    set_values: Annotated[
        list[str],
        typer.Option(
            "--set",
            help="Corrected value as field=value; repeat for several fields.",
        ),
    ],
    note: Annotated[
        str,
        typer.Option("--note", help="Why the corrected value is right (required)."),
    ],
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
) -> None:
    """Record a correction for a held row, headless.

    The correction is validated exactly as the dashboard form validates it —
    each value must coerce to its field's declared type and pass that
    field's rules — then appended to runs/resolutions.jsonl. Nothing is
    applied until the next 'muster run', which re-validates the whole row
    against the full rule set before the row rejoins the governed dataset.
    A newer record for the same fingerprint supersedes an older one.
    """
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    if not note.strip():
        errors.print("--note is required: say why the corrected value is right.")
        raise typer.Exit(code=1)
    if len(note) > MAX_NOTE_LENGTH:
        errors.print(f"--note must be at most {MAX_NOTE_LENGTH} characters.")
        raise typer.Exit(code=1)
    corrected_values: dict[str, str] = {}
    for pair in set_values:
        field, separator, value = pair.partition("=")
        if not separator or not field.strip():
            errors.print(f"--set expects field=value, got '{pair}'.")
            raise typer.Exit(code=1)
        corrected_values[field.strip()] = value.strip()

    root = config_path.resolve().parent
    target = next(
        (row for row in load_exceptions(root, config) if row.id == fingerprint), None
    )
    if target is None:
        errors.print(
            f"no exception with fingerprint '{fingerprint}' in the latest run; "
            "see the dashboard's exceptions browser for current fingerprints."
        )
        raise typer.Exit(code=1)
    if target.severity != "error" or not target.row:
        errors.print(
            "only row-level errors can be corrected; this exception is "
            f"severity '{target.severity}'"
            + ("" if target.row else " with no row number")
            + "."
        )
        raise typer.Exit(code=1)
    failures = check_correction(config, corrected_values)
    if failures:
        errors.print("correction rejected:")
        for failure in failures:
            errors.print(f"  - {failure}")
        raise typer.Exit(code=1)
    append_resolution(root, fingerprint, "corrected", note.strip(), corrected_values)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(corrected_values.items()))
    console.print(
        f"Correction recorded for {fingerprint} ({summary}); it applies — and "
        "the whole row is re-validated — on the next 'muster run'."
    )


@app.command()
def publish(
    target: Annotated[
        str | None,
        typer.Argument(
            help="Target from 'targets:' in muster.yaml; optional when only "
            "one is configured."
        ),
    ] = None,
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print what would happen; write nothing."),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Publish even though the latest run recorded error-severity "
            "exceptions (recorded loudly in the manifest chain).",
        ),
    ] = False,
) -> None:
    """Publish the latest governed dataset to a configured target.

    The dataset is verified against the latest run manifest before anything
    is sent, a run with error-severity exceptions is refused without
    --force, and every publish appends to the tamper-evident manifest
    chain. Exits 0 when everything landed, 2 when some records failed
    (see publish-exceptions.csv), and 1 when the publish could not proceed.
    """
    try:
        config = load_config(config_path)
        root = config_path.resolve().parent
        result = publish_dataset(
            config, root, target, dry_run=dry_run, force=force
        )
    except (ConfigError, PublishError) as exc:
        errors.print(redact_text(str(exc)))
        raise typer.Exit(code=1) from exc
    if result.forced:
        errors.print(
            "FORCED: the source run recorded error-severity exceptions and "
            "--force was given; this override is recorded in the manifest chain."
        )
    if dry_run:
        console.print(
            f"Dry run for target '{result.target}' ({result.target_type}) — "
            "nothing will be written:"
        )
        for line in result.plan:
            console.print(f"  - {line}")
        console.print(f"Dataset: {result.rows} row(s) from run {result.source_run}.")
        return
    console.print(
        f"Published {result.rows_sent} of {result.rows} row(s) to target "
        f"'{result.target}' — {redact_text(result.destination)} — in "
        f"{result.duration_seconds:.1f}s."
    )
    if result.rows_failed:
        errors.print(
            f"{result.rows_failed} record(s) failed; see {result.exceptions_csv}."
        )
    console.print(f"  manifest: {result.manifest_path}")
    if result.rows_failed:
        raise typer.Exit(code=2)


@app.command()
def schedule(
    expression: Annotated[
        str | None,
        typer.Argument(
            help='Five-field cron expression, e.g. "*/15 * * * *", or an '
            "alias like @hourly."
        ),
    ] = None,
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
    print_units: Annotated[
        bool,
        typer.Option(
            "--print",
            help="Print a ready-to-use systemd unit and cron line instead of "
            "relying on the muster daemon.",
        ),
    ] = False,
) -> None:
    """Set (or show) the cron schedule the daemon runs the pipeline on.

    The schedule is stored in muster.schedule beside muster.yaml and picked
    up by a running daemon without a restart. Prefer the operating system's
    scheduler where available: --print emits the equivalent systemd unit
    and crontab line.
    """
    root = config_path.resolve().parent
    try:
        if expression is not None:
            parsed = CronExpression.parse(expression)
            path = write_schedule(root, parsed)
            console.print(f"Schedule '{parsed.raw}' written to {path}.")
        else:
            parsed = read_schedule(root)
            console.print(f"Current schedule: '{parsed.raw}'")
    except SchedulerError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    moment = datetime.now()
    fires = []
    for _ in range(3):
        moment = parsed.next_after(moment)
        fires.append(moment.isoformat(sep=" ", timespec="minutes"))
    console.print(f"Next three runs: {', '.join(fires)}")
    if print_units:
        console.print("\n# systemd unit — save as ~/.config/systemd/user/muster.service")
        console.print(systemd_unit(root))
        console.print("# or a crontab line (crontab -e):")
        console.print(cron_line(root, parsed))
    else:
        console.print(
            "Start the daemon with 'muster daemon start' (or run 'muster "
            "schedule --print' for systemd/cron units)."
        )


@app.command()
def daemon(
    action: Annotated[
        str,
        typer.Argument(help="start, stop, status, or run (foreground loop)."),
    ],
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
) -> None:
    """Control the scheduling daemon.

    'start' launches the loop detached with a PID file under runs/ and a
    size-rotated runs/daemon.log; 'run' keeps it in the foreground (what a
    systemd unit should call). A failed scheduled run is recorded with its
    exit code, and notified to the webhook named by MUSTER_WEBHOOK_URL when
    that variable is set.
    """
    root = config_path.resolve().parent
    runs_dir = root / RUNS_DIRECTORY
    try:
        if action == "start":
            pid = start_daemon(root, config_path.resolve(), runs_dir)
            console.print(
                f"Daemon started with PID {pid}; schedule "
                f"'{read_schedule(root).raw}', log {runs_dir / 'daemon.log'}."
            )
        elif action == "stop":
            pid = stop_daemon(runs_dir)
            console.print(f"Daemon with PID {pid} stopped.")
        elif action == "status":
            running = daemon_pid(runs_dir)
            if running is None:
                console.print("Daemon is not running.")
                raise typer.Exit(code=3)  # mirrors systemctl's convention
            parsed = read_schedule(root)
            console.print(
                f"Daemon running with PID {running}; schedule '{parsed.raw}', "
                f"next run {parsed.next_after(datetime.now()).isoformat(sep=' ', timespec='minutes')}."
            )
        elif action == "run":
            configure_daemon_logging(runs_dir)
            daemon_loop(root, config_path.resolve())
        else:
            errors.print(f"unknown action '{action}': use start, stop, status or run.")
            raise typer.Exit(code=1)
    except SchedulerError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc


@app.command()
def serve(
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
    host: Annotated[
        str,
        typer.Option(
            help="Address to bind. The default is local-only; binding "
            "anything else exposes the dashboard to that network."
        ),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8600,
) -> None:
    """Serve the local dashboard: runs, remediation, trends, publishing, report.

    Local-first: binds 127.0.0.1 unless --host says otherwise (with a
    printed warning). A single login token is generated on first serve and
    stored in the OS keyring (or an owner-only file when no keyring backend
    exists); every page requires it.
    """
    import uvicorn

    from muster.web import create_app
    from muster.web.auth import load_or_create_token

    try:
        load_config(config_path)  # fail before binding, with a clear message
    except ConfigError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    root = config_path.resolve().parent
    if host != "127.0.0.1":
        errors.print(
            f"WARNING: binding {host} exposes the dashboard beyond this "
            "machine. Muster's web interface is designed to be local-first; "
            "prefer an SSH tunnel over a wider bind."
        )
    token, token_home = load_or_create_token(root)
    console.print(f"Login token (stored in {token_home}):\n  {token}")
    console.print(f"Dashboard: http://{host}:{port}/  (Ctrl+C stops the server)")
    uvicorn.run(
        create_app(root, config_path.resolve()),
        host=host,
        port=port,
        log_level="warning",
    )


@app.command()
def report(
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
    run_id: Annotated[
        str | None,
        typer.Option("--run", help="Run to render; defaults to the latest."),
    ] = None,
    output: Annotated[
        Path | None, typer.Option(help="Where to write report.html.")
    ] = None,
) -> None:
    """Re-render the HTML report for a past run from its archived data."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    root = config_path.resolve().parent
    runs_dir = root / RUNS_DIRECTORY
    run_dir: Path | None
    if run_id:
        run_dir = runs_dir / run_id
    else:
        # Publish manifests share the runs directory; report on the latest
        # pipeline run, not the latest chain entry.
        found = latest_manifest_of_kind(runs_dir, "run")
        run_dir = found[0].parent if found else None
    if run_dir is None or not (run_dir / REPORT_DATA_NAME).is_file():
        errors.print(
            f"no run data found under {runs_dir}; run 'muster run' first"
            + (f" (looked for run '{run_id}')" if run_id else "")
        )
        raise typer.Exit(code=1)
    data = RunReportData.from_json(
        (run_dir / REPORT_DATA_NAME).read_text(encoding="utf-8")
    )
    destination = output or root / config.output.directory / "report.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_report(data, destination)
    console.print(f"Rendered run {run_dir.name} to {destination}.")
