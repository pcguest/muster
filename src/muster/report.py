"""Build and render the run report: Muster's public face.

The report is one self-contained HTML file — inline CSS, no external
requests, no scripts — legible to a non-technical reader and printable. Every
value that originated in a source file is HTML-escaped before rendering:
input files are untrusted, and a spreadsheet cell must never be able to
inject markup into the report.

Each run also archives its report data as JSON inside the run directory, so
``muster report`` can re-render any past run without re-reading the sources.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from html import escape
from pathlib import Path

import polars as pl

from muster import __version__
from muster.config import Config
from muster.mapping import ColumnMatch
from muster.records import ExceptionRecord

logger = logging.getLogger(__name__)

REPORT_DATA_NAME = "report.json"


@dataclass
class FieldQuality:
    name: str
    type: str
    required: bool
    completeness_pct: float | None  # non-empty share of published rows
    validity_pct: float | None  # rows free of violations for this field


@dataclass
class SourceQuality:
    file: str
    rows: int
    errors: int
    warnings: int
    score: int  # 0-100; errors weigh 1 row, warnings a quarter


@dataclass
class MappingDecision:
    file: str
    source_column: str
    target: str | None
    method: str | None
    score: float | None
    reason: str | None


@dataclass
class ExceptionSummary:
    kind: str
    errors: int
    warnings: int


@dataclass
class ConflictDetail:
    files: str
    column: str
    values: str
    reason: str


@dataclass
class RunReportData:
    run_id: str
    generated_at: str
    muster_version: str
    config_file: str
    config_sha256: str
    duration_seconds: float
    files_read: int
    rows_in: int
    rows_published: int
    rows_held: int
    rows_superseded: int
    error_count: int
    warning_count: int
    conflicts_held: int
    fields: list[FieldQuality] = field(default_factory=list)
    sources: list[SourceQuality] = field(default_factory=list)
    mappings: list[MappingDecision] = field(default_factory=list)
    exception_kinds: list[ExceptionSummary] = field(default_factory=list)
    conflicts: list[ConflictDetail] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> RunReportData:
        raw = json.loads(text)
        raw["fields"] = [FieldQuality(**item) for item in raw.get("fields", [])]
        raw["sources"] = [SourceQuality(**item) for item in raw.get("sources", [])]
        raw["mappings"] = [MappingDecision(**item) for item in raw.get("mappings", [])]
        raw["exception_kinds"] = [
            ExceptionSummary(**item) for item in raw.get("exception_kinds", [])
        ]
        raw["conflicts"] = [ConflictDetail(**item) for item in raw.get("conflicts", [])]
        return cls(**raw)


def _field_metrics(
    config: Config,
    published: pl.DataFrame,
    rows_in: int,
    exceptions: Sequence[ExceptionRecord],
    mappings: Sequence[tuple[str, ColumnMatch]],
) -> list[FieldQuality]:
    field_names = {spec.name for spec in config.fields}
    source_to_field = {
        (file, match.source): match.target
        for file, match in mappings
        if match.target is not None
    }
    violating_rows: dict[str, set[tuple[str, int]]] = {name: set() for name in field_names}
    for record in exceptions:
        if record.row is None or record.column is None:
            continue
        name = (
            record.column
            if record.column in field_names
            else source_to_field.get((record.file, record.column))
        )
        if name is not None:
            violating_rows[name].add((record.file, record.row))

    metrics = []
    for spec in config.fields:
        if published.height:
            non_empty = published.height - published.get_column(spec.name).null_count()
            completeness = round(100 * non_empty / published.height, 1)
        else:
            completeness = None
        if rows_in:
            validity = round(100 * (1 - len(violating_rows[spec.name]) / rows_in), 1)
        else:
            validity = None
        metrics.append(
            FieldQuality(spec.name, spec.type, spec.required, completeness, validity)
        )
    return metrics


def _source_metrics(
    file_rows: Mapping[str, int], exceptions: Sequence[ExceptionRecord]
) -> list[SourceQuality]:
    metrics = []
    for file, rows in file_rows.items():
        errors = {r.row for r in exceptions if r.file == file and r.severity == "error"}
        error_rows = len({row for row in errors if row is not None})
        file_level_errors = 1 if None in errors else 0
        warnings = sum(
            1 for r in exceptions if r.file == file and r.severity == "warning"
        )
        if rows == 0:
            score = 0 if file_level_errors else 100
        else:
            penalty = (error_rows + 0.25 * warnings) / rows
            score = max(0, round(100 * (1 - penalty)))
            if file_level_errors:
                score = 0
        metrics.append(SourceQuality(file, rows, error_rows + file_level_errors, warnings, score))
    return metrics


def build_report(
    *,
    config: Config,
    published: pl.DataFrame,
    file_rows: Mapping[str, int],
    mappings: Sequence[tuple[str, ColumnMatch]],
    exceptions: Sequence[ExceptionRecord],
    run_id: str,
    generated_at: str,
    config_file: str,
    config_sha256: str,
    duration_seconds: float,
    rows_in: int,
    rows_published: int,
    rows_held: int,
    rows_superseded: int,
    conflicts_held: int,
) -> RunReportData:
    """Assemble everything the report shows from one run's artefacts."""
    kinds: dict[str, ExceptionSummary] = {}
    for record in exceptions:
        summary = kinds.setdefault(record.kind, ExceptionSummary(record.kind, 0, 0))
        if record.severity == "error":
            summary.errors += 1
        else:
            summary.warnings += 1

    conflicts = [
        ConflictDetail(
            files=record.file,
            column=record.column or "",
            values=record.value or "",
            reason=record.reason,
        )
        for record in exceptions
        if record.kind == "conflict" and record.severity == "error"
    ]

    return RunReportData(
        run_id=run_id,
        generated_at=generated_at,
        muster_version=__version__,
        config_file=config_file,
        config_sha256=config_sha256,
        duration_seconds=duration_seconds,
        files_read=len(file_rows),
        rows_in=rows_in,
        rows_published=rows_published,
        rows_held=rows_held,
        rows_superseded=rows_superseded,
        error_count=sum(1 for r in exceptions if r.severity == "error"),
        warning_count=sum(1 for r in exceptions if r.severity == "warning"),
        conflicts_held=conflicts_held,
        fields=_field_metrics(config, published, rows_in, exceptions, mappings),
        sources=_source_metrics(file_rows, exceptions),
        mappings=[
            MappingDecision(
                file, match.source, match.target, match.method, match.score, match.reason
            )
            for file, match in mappings
        ],
        exception_kinds=sorted(
            kinds.values(), key=lambda s: (-(s.errors + s.warnings), s.kind)
        ),
        conflicts=conflicts,
    )


_CSS = """
:root {
  --bg: #0c1017; --panel: #131a24; --edge: #202b3a; --ink: #dce5f0;
  --dim: #8494a8; --good: #3fce8b; --warn: #e8b33d; --bad: #f26d6d;
  --accent: #56b8e6; --mono: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
}
* { box-sizing: border-box; margin: 0; }
body {
  background: var(--bg); color: var(--ink); padding: 2.2rem clamp(1rem, 5vw, 3.5rem);
  font: 15px/1.55 -apple-system, "Segoe UI", system-ui, sans-serif;
}
header { border-bottom: 1px solid var(--edge); padding-bottom: 1.1rem; margin-bottom: 1.6rem; }
header h1 { font-size: 1.45rem; font-weight: 650; letter-spacing: .01em; }
header h1 span { color: var(--accent); }
.meta { color: var(--dim); font-size: .82rem; margin-top: .35rem; }
.meta code { font-family: var(--mono); font-size: .78rem; color: var(--ink); }
section { margin-bottom: 1.9rem; }
h2 {
  font-size: .8rem; font-weight: 650; text-transform: uppercase; letter-spacing: .12em;
  color: var(--dim); margin-bottom: .7rem;
}
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .7rem; }
.kpi {
  background: var(--panel); border: 1px solid var(--edge); border-radius: 8px;
  padding: .8rem 1rem;
}
.kpi .n { font-family: var(--mono); font-size: 1.7rem; font-weight: 600; }
.kpi .l { color: var(--dim); font-size: .74rem; text-transform: uppercase; letter-spacing: .09em; }
.kpi.good .n { color: var(--good); } .kpi.warn .n { color: var(--warn); }
.kpi.bad .n { color: var(--bad); } .kpi.plain .n { color: var(--ink); }
.panel {
  background: var(--panel); border: 1px solid var(--edge); border-radius: 8px;
  padding: .4rem 1rem .6rem; overflow-x: auto;
}
table { border-collapse: collapse; width: 100%; font-size: .86rem; }
th, td { text-align: left; padding: .45rem .7rem .45rem 0; vertical-align: top; }
th {
  color: var(--dim); font-size: .72rem; text-transform: uppercase; letter-spacing: .09em;
  font-weight: 600; border-bottom: 1px solid var(--edge);
}
td { border-bottom: 1px solid var(--edge); }
tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.bar {
  display: inline-block; width: 110px; height: 7px; border-radius: 4px;
  background: var(--edge); vertical-align: middle; margin-right: .55rem;
}
.bar i { display: block; height: 100%; border-radius: 4px; }
.good-fill { background: var(--good); } .warn-fill { background: var(--warn); }
.bad-fill { background: var(--bad); }
.tag {
  font-family: var(--mono); font-size: .72rem; padding: .1rem .45rem;
  border-radius: 4px; border: 1px solid var(--edge); color: var(--dim);
}
.tag.exact { color: var(--good); border-color: var(--good); }
.tag.synonym { color: var(--accent); border-color: var(--accent); }
.tag.fuzzy { color: var(--warn); border-color: var(--warn); }
.tag.assist { color: var(--accent); border-color: var(--accent); }
.tag.unmapped { color: var(--bad); border-color: var(--bad); }
.sev-error { color: var(--bad); font-weight: 600; }
.sev-warning { color: var(--warn); font-weight: 600; }
.empty { color: var(--dim); font-style: italic; padding: .6rem 0; }
.values { font-family: var(--mono); font-size: .78rem; word-break: break-word; }
footer {
  border-top: 1px solid var(--edge); margin-top: 2.2rem; padding-top: 1rem;
  color: var(--dim); font-size: .78rem;
}
@media print {
  :root {
    --bg: #ffffff; --panel: #ffffff; --edge: #c9d2dc; --ink: #16202c;
    --dim: #55636f; --good: #157a4e; --warn: #8a6414; --bad: #b03434; --accent: #1668a0;
  }
  body { padding: 0; font-size: 12px; }
  .panel, .kpi { break-inside: avoid; }
  section { break-inside: avoid-page; }
}
"""


def _pct_cell(value: float | None) -> str:
    if value is None:
        return '<td class="num">—</td>'
    fill = "good-fill" if value >= 95 else "warn-fill" if value >= 80 else "bad-fill"
    return (
        f'<td class="num"><span class="bar"><i class="{fill}" '
        f'style="width:{value:.1f}%"></i></span>{value:.1f}%</td>'
    )


def _score_cell(score: int) -> str:
    fill = "good-fill" if score >= 95 else "warn-fill" if score >= 80 else "bad-fill"
    return (
        f'<td class="num"><span class="bar"><i class="{fill}" '
        f'style="width:{score}%"></i></span>{score}</td>'
    )


def _kpi(label: str, value: object, tone: str) -> str:
    return (
        f'<div class="kpi {tone}"><div class="n">{escape(str(value))}</div>'
        f'<div class="l">{escape(label)}</div></div>'
    )


def _count_cell(count: int, severity: str) -> str:
    if not count:
        return '<td class="num">0</td>'
    return f'<td class="num"><span class="sev-{severity}">{count}</span></td>'


def _mapping_tag(decision: MappingDecision) -> str:
    if decision.target is None:
        return '<span class="tag unmapped">unmapped</span>'
    label = decision.method or ""
    if decision.method == "fuzzy" and decision.score is not None:
        label = f"fuzzy {decision.score:.0f}"
    return f'<span class="tag {escape(decision.method or "")}">{escape(label)}</span>'


def render_report(data: RunReportData) -> str:
    """Render the report data to a single self-contained HTML document."""
    held_tone = "bad" if data.rows_held else "good"
    error_tone = "bad" if data.error_count else "good"
    warn_tone = "warn" if data.warning_count else "good"
    kpis = "\n".join(
        [
            _kpi("rows in", data.rows_in, "plain"),
            _kpi("rows published", data.rows_published, "good"),
            _kpi("rows held", data.rows_held, held_tone),
            _kpi("rows superseded", data.rows_superseded, "plain"),
            _kpi("errors", data.error_count, error_tone),
            _kpi("warnings", data.warning_count, warn_tone),
        ]
    )

    field_rows = "\n".join(
        f"<tr><td>{escape(f.name)}</td><td>{escape(f.type)}</td>"
        f"<td>{'yes' if f.required else ''}</td>"
        f"{_pct_cell(f.completeness_pct)}{_pct_cell(f.validity_pct)}</tr>"
        for f in data.fields
    )

    source_rows = "\n".join(
        f"<tr><td>{escape(s.file)}</td><td class='num'>{s.rows}</td>"
        f"<td class='num'>{s.errors}</td><td class='num'>{s.warnings}</td>"
        f"{_score_cell(s.score)}</tr>"
        for s in data.sources
    )

    # Plain string concatenation here: nested quotes and escapes inside
    # f-strings are Python 3.12+ syntax, and Muster supports 3.11.
    empty_cell = '<span class="empty">—</span>'
    mapping_rows = "\n".join(
        f"<tr><td>{escape(m.file)}</td><td>{escape(m.source_column)}</td>"
        f"<td>{escape(m.target) if m.target else empty_cell}</td>"
        f"<td>{_mapping_tag(m)}</td>"
        f"<td>{escape(m.reason or '')}</td></tr>"
        for m in data.mappings
    )

    kind_rows = "\n".join(
        f"<tr><td>{escape(k.kind)}</td>"
        f"{_count_cell(k.errors, 'error')}{_count_cell(k.warnings, 'warning')}</tr>"
        for k in data.exception_kinds
    ) or '<tr><td colspan="3" class="empty">No exceptions — a clean run.</td></tr>'

    conflict_rows = "\n".join(
        f"<tr><td>{escape(c.column)}</td><td class='values'>{escape(c.values)}</td>"
        f"<td>{escape(c.reason)}</td></tr>"
        for c in data.conflicts
    ) or '<tr><td colspan="3" class="empty">No conflicts held for review.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Muster run report — {escape(data.run_id)}</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <h1><span>Muster</span> run report</h1>
  <div class="meta">
    Run <code>{escape(data.run_id)}</code> · {escape(data.generated_at)} ·
    {data.duration_seconds:.2f}s · config <code>{escape(data.config_file)}</code>
    (sha256 <code>{escape(data.config_sha256[:12])}…</code>)
  </div>
</header>

<section>
  <h2>Run summary</h2>
  <div class="kpis">
{kpis}
  </div>
</section>

<section>
  <h2>Field quality — governed dataset</h2>
  <div class="panel">
  <table>
    <tr><th>Field</th><th>Type</th><th>Required</th>
        <th class="num">Completeness</th><th class="num">Validity</th></tr>
{field_rows}
  </table>
  </div>
</section>

<section>
  <h2>Source file quality</h2>
  <div class="panel">
  <table>
    <tr><th>File</th><th class="num">Rows</th><th class="num">Errors</th>
        <th class="num">Warnings</th><th class="num">Score</th></tr>
{source_rows}
  </table>
  </div>
</section>

<section>
  <h2>Mapping decisions</h2>
  <div class="panel">
  <table>
    <tr><th>File</th><th>Source column</th><th>Mapped to</th><th>How</th><th>Note</th></tr>
{mapping_rows}
  </table>
  </div>
</section>

<section>
  <h2>Exceptions by kind</h2>
  <div class="panel">
  <table>
    <tr><th>Kind</th><th class="num">Errors</th><th class="num">Warnings</th></tr>
{kind_rows}
  </table>
  </div>
</section>

<section>
  <h2>Conflicts held for review</h2>
  <div class="panel">
  <table>
    <tr><th>Field</th><th>Each source's value</th><th>Why held</th></tr>
{conflict_rows}
  </table>
  </div>
</section>

<footer>
  Errors hold a row out of the governed dataset; warnings are published and
  reported. Source scores weigh an error row as 1 and a warning as ¼ of a row.
  This run's manifest is hash-chained to its predecessor — see runs/{escape(data.run_id)}/manifest.json.
  Generated by Muster v{escape(data.muster_version)}.
</footer>
</body>
</html>
"""


def write_report(data: RunReportData, html_path: Path, data_path: Path | None = None) -> None:
    """Write report.html, and optionally archive the data alongside the run."""
    html_path.write_text(render_report(data), encoding="utf-8")
    if data_path is not None:
        data_path.write_text(data.to_json(), encoding="utf-8")
    logger.info("wrote report path=%s", html_path)
