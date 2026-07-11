"""Command-line interface: init, confirm, profile, run, review and report."""

import dataclasses
import json
import logging
from pathlib import Path
from typing import Annotated, Optional

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
from muster.logs import configure_logging
from muster.manifest import RUNS_DIRECTORY, latest_run_directory
from muster.pipeline import PipelineError, run_pipeline
from muster.profiling import FileProfile, profile_folder
from muster.report import REPORT_DATA_NAME, RunReportData, write_report
from muster.scaffold import confirm_text, propose_config

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
        Optional[Path],
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
    console.print(
        f"Published {result.rows_published} of {result.rows_in} row(s) from "
        f"{result.files_read} file(s); {result.rows_held} held, "
        f"{result.rows_superseded} superseded."
    )
    console.print(f"  dataset:    {result.output_parquet} and {result.output_csv}")
    console.print(
        f"  exceptions: {result.error_count} error(s), {result.warning_count} "
        f"warning(s) in {result.exceptions_csv}"
    )
    console.print(f"  report:     {result.report_html}")
    console.print(f"  manifest:   {result.manifest_path}")
    if assist:
        _propose_and_write(config, root, result.unmapped_samples)
    if result.error_count:
        raise typer.Exit(code=2)


@app.command()
def review(
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
    review_file: Annotated[
        Optional[Path],
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
def report(
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
    run_id: Annotated[
        Optional[str],
        typer.Option("--run", help="Run to render; defaults to the latest."),
    ] = None,
    output: Annotated[
        Optional[Path], typer.Option(help="Where to write report.html.")
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
    run_dir = runs_dir / run_id if run_id else latest_run_directory(runs_dir)
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
