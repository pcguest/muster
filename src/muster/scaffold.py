"""Generate a proposed configuration from real source files.

``muster init --from <folder>`` profiles the folder, clusters column
headings that look like variants of one another, and writes a muster.yaml
proposing a canonical schema: the most common name variant per cluster,
the inferred type, and every observed heading as a synonym. Every inference
carries a ``# PROPOSED`` marker and Muster refuses to run while any marker
remains — a generated file never silently becomes the configuration of
record. The user confirms by removing markers as they review, or by running
``muster confirm`` to accept the lot.

Headings come from untrusted files, so every string written into the YAML
is JSON-quoted (valid YAML, safe against crafted headings that would
otherwise inject structure).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import yaml
from rapidfuzz import fuzz

from muster import __version__
from muster.config import Config, ConfigError
from muster.mapping import normalise
from muster.profiling import FileProfile

logger = logging.getLogger(__name__)

# The textual marker that keeps a generated configuration out of service.
PROPOSED_RE = re.compile(r"#\s*PROPOSED\b")
# A marker plus everything after it, for confirmation stripping.
_STRIP_RE = re.compile(r"\s*#\s*PROPOSED\b.*$")

# Two headings whose normalised forms score at least this are treated as
# variants of one canonical field. Deliberately strict: a missed join gives
# two visible proposals the reviewer can merge, while a wrong join could
# smuggle a bad synonym past a hasty review.
CLUSTER_THRESHOLD = 90.0


@dataclass
class _Observation:
    file: str
    rows: int
    non_empty: int
    inferred_type: str


@dataclass
class _Cluster:
    # variant heading -> observations of it, in file order
    variants: dict[str, list[_Observation]] = field(default_factory=dict)

    def add(self, heading: str, observation: _Observation) -> None:
        self.variants.setdefault(heading, []).append(observation)

    def score(self, heading: str) -> float:
        norm = normalise(heading)
        return max(fuzz.ratio(norm, normalise(v)) for v in self.variants)

    def observations(self) -> list[_Observation]:
        return [o for obs in self.variants.values() for o in obs]

    def canonical_variant(self) -> str:
        # The variant seen in the most files; ties go to the earliest seen.
        return max(self.variants, key=lambda v: len(self.variants[v]))


def _cluster_headings(profiles: list[FileProfile]) -> list[_Cluster]:
    clusters: list[_Cluster] = []
    for profile in profiles:
        for column in profile.columns:
            observation = _Observation(
                file=profile.file,
                rows=profile.rows,
                non_empty=column.non_empty,
                inferred_type=column.inferred_type,
            )
            best: _Cluster | None = None
            best_score = 0.0
            for cluster in clusters:
                score = cluster.score(column.name)
                if score > best_score:
                    best, best_score = cluster, score
            if best is not None and best_score >= CLUSTER_THRESHOLD:
                best.add(column.name, observation)
            else:
                clusters.append(_Cluster(variants={column.name: [observation]}))
    return clusters


def _snake_case(heading: str) -> str:
    return normalise(heading).replace(" ", "_")


def _propose_type(cluster: _Cluster) -> tuple[str, str]:
    """The proposed type and the rationale for it."""
    observed = {o.inferred_type for o in cluster.observations() if o.non_empty}
    if not observed:
        return "string", "no values seen; defaulting to string"
    if len(observed) == 1:
        kind = observed.pop()
        return kind, f"every profiled value reads as {kind}"
    if observed <= {"integer", "float"}:
        return "float", "mixed integer and float values; float covers both"
    listing = ", ".join(sorted(observed))
    return "string", f"files disagree ({listing}); string is the safe reading"


def _propose_required(
    cluster: _Cluster, file_count: int
) -> tuple[bool, str]:
    observations = cluster.observations()
    files_seen = {o.file for o in observations}
    if len(files_seen) < file_count:
        return False, f"seen in {len(files_seen)} of {file_count} files"
    if any(o.non_empty < o.rows for o in observations):
        return False, "empty values seen"
    if all(o.rows == 0 for o in observations):
        return False, "no data rows to judge from"
    return True, f"present and non-empty in all {file_count} files"


def _quoted(value: str) -> str:
    # JSON strings are valid YAML scalars; headings are untrusted input.
    return json.dumps(value, ensure_ascii=False)


def propose_config(profiles: list[FileProfile], folder: str) -> str:
    """Render a proposed muster.yaml from folder profiles.

    ``folder`` is the source folder as the user gave it, used for the
    sources globs; it should be relative to where the config will live.
    """
    usable = [p for p in profiles if p.error is None]
    if not usable:
        raise ConfigError(
            "no readable source files to propose a configuration from"
        )
    skipped = [p for p in profiles if p.error is not None]
    clusters = _cluster_headings(usable)

    lines: list[str] = [
        f"# muster.yaml — proposed by 'muster init --from {folder}' (muster {__version__}).",
        "#",
        "# Every inference below is marked for review, and muster will refuse to",
        "# run while any marker remains: a generated file is a proposal, never",
        "# silently the configuration of record. Review each inference and delete",
        "# its marker — or run 'muster confirm' to accept them all — and correct",
        "# anything the profiler read wrongly; it sees your files, not your",
        "# intent.",
    ]
    for profile in skipped:
        lines.append(f"# NOTE: {profile.file} was not profiled: {profile.error}")
    lines.append("")
    lines.append("fields:")

    used_names: set[str] = set()
    for index, cluster in enumerate(clusters):
        variant = cluster.canonical_variant()
        name = _snake_case(variant) or f"column_{index + 1}"
        while name in used_names:
            name += "_2"
        used_names.add(name)

        kind, type_reason = _propose_type(cluster)
        required, required_reason = _propose_required(cluster, len(usable))
        seen_in = len({o.file for o in cluster.variants[variant]})
        lines.append(
            f"  - name: {name}  "
            f"# PROPOSED: from {_quoted(variant)} ({seen_in} of {len(usable)} files)"
        )
        lines.append(f"    type: {kind}  # PROPOSED: {type_reason}")
        lines.append(
            f"    required: {'true' if required else 'false'}  # PROPOSED: {required_reason}"
        )
        synonyms = [v for v in cluster.variants if v != name]
        listing = ", ".join(_quoted(v) for v in synonyms)
        lines.append(f"    synonyms: [{listing}]  # PROPOSED: observed headings")

    folder_glob = folder.rstrip("/")
    lines += [
        "",
        "sources:",
        f"  - {_quoted(folder_glob + '/**/*.csv')}  # PROPOSED: from the --from folder",
        f"  - {_quoted(folder_glob + '/**/*.xlsx')}  # PROPOSED: from the --from folder",
        "",
        "matching:",
        "  fuzzy_threshold: 90",
        "",
        "# Set keys to the column(s) that identify a record to enable duplicate",
        "# detection and reconciliation across files, e.g. keys: [\"customer_id\"].",
        "# Add per-field rules and cross_field checks once the schema is agreed;",
        "# see 'muster init' (without --from) for a commented example.",
        "validation:",
        "  keys: []",
        "  cross_field: []",
        "",
        "limits:",
        "  max_file_size_mb: 100",
        "  chunk_rows: 100000",
        "",
        "output:",
        "  directory: output",
        "  dataset_name: consolidated",
        "",
    ]
    text = "\n".join(lines)
    # The proposal must at least be a loadable configuration once confirmed.
    Config.model_validate(yaml.safe_load(text))
    logger.info(
        "proposed config fields=%d files=%d skipped=%d",
        len(clusters),
        len(usable),
        len(skipped),
    )
    return text


def confirm_text(text: str) -> tuple[str, int]:
    """Strip every PROPOSED marker; return the confirmed text and the count.

    The stripped text must parse as a valid configuration or
    :class:`ConfigError` is raised and nothing should be written back.
    """
    lines = []
    count = 0
    for line in text.splitlines():
        if PROPOSED_RE.search(line):
            count += 1
            if line.lstrip().startswith("#"):
                continue  # a whole-line marker comment disappears
            line = _STRIP_RE.sub("", line)
        lines.append(line)
    confirmed = "\n".join(lines) + "\n"
    if count:
        try:
            Config.model_validate(yaml.safe_load(confirmed))
        except Exception as exc:
            raise ConfigError(
                f"configuration is not valid after removing markers: {exc}"
            ) from exc
    return confirmed, count
