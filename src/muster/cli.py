"""Command-line interface: muster init, profile and run."""

import dataclasses
import json
import logging
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from muster import __version__
from muster.config import CONFIG_TEMPLATE, ConfigError, load_config
from muster.logs import configure_logging
from muster.pipeline import PipelineError, run_pipeline
from muster.profiling import FileProfile, profile_folder

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
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing file.")
    ] = False,
) -> None:
    """Write a starter muster.yaml with an example canonical schema."""
    if path.exists() and not force:
        errors.print(f"{path} already exists; pass --force to overwrite it.")
        raise typer.Exit(code=1)
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    console.print(
        f"Wrote {path}. Edit the canonical schema and sources, then run 'muster run'."
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


@app.command()
def run(
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to muster.yaml.")
    ] = Path("muster.yaml"),
) -> None:
    """Consolidate all configured sources into one governed dataset."""
    try:
        config = load_config(config_path)
        result = run_pipeline(config, config_path.resolve().parent)
    except (ConfigError, PipelineError) as exc:
        errors.print(str(exc))
        raise typer.Exit(code=1) from exc
    console.print(
        f"Consolidated {result.rows_out} row(s) from {result.files_read} file(s)."
    )
    console.print(f"  dataset:    {result.output_parquet} and {result.output_csv}")
    console.print(
        f"  exceptions: {result.exception_count} recorded in {result.exceptions_csv}"
    )
