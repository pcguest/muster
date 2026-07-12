"""Map source column headings onto the canonical schema.

Matching runs in three stages of decreasing confidence:

1. exact — the heading equals a canonical field name verbatim;
2. synonym — the heading equals a declared synonym (or canonical name),
   compared case- and punctuation-insensitively;
3. assist — the heading matches a mapping a person accepted in the
   assisted-mapping review file (LLM-proposed, human-approved);
4. fuzzy — the normalised heading is compared to every canonical name and
   synonym with rapidfuzz; the best score at or above the threshold wins.

Each canonical field maps from at most one source column per file. A column
that matches nothing, ties between two fields, or targets a field already
claimed is returned unmapped with a written reason — never guessed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rapidfuzz import fuzz

from muster.config import FieldSpec

logger = logging.getLogger(__name__)

_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """Lower-case and collapse punctuation and whitespace to single spaces."""
    return _NORMALISE_RE.sub(" ", text.lower()).strip()


@dataclass(frozen=True)
class ColumnMatch:
    """The outcome of matching one source column."""

    source: str
    target: str | None
    method: str | None  # "exact", "synonym" or "fuzzy"
    score: float | None
    reason: str | None  # populated when target is None


def map_columns(
    source_columns: Sequence[str],
    fields: Sequence[FieldSpec],
    threshold: float,
    accepted: Mapping[str, str] | None = None,
) -> list[ColumnMatch]:
    """Match source columns to canonical fields, one column per field.

    ``accepted`` maps normalised headings to fields, from human-approved
    assisted-mapping proposals; it ranks below declared synonyms and above
    fuzzy matching.
    """
    exact_names = {field.name: field.name for field in fields}
    synonym_lookup: dict[str, str] = {}
    for field in fields:
        for label in (field.name, *field.synonyms):
            synonym_lookup.setdefault(normalise(label), field.name)

    matched: dict[str, ColumnMatch] = {}
    claimed: dict[str, str] = {}  # canonical field -> source column

    def claim(source: str, target: str, method: str, score: float) -> None:
        if target in claimed:
            matched[source] = ColumnMatch(
                source,
                None,
                None,
                None,
                f"field '{target}' is already mapped from column '{claimed[target]}'",
            )
        else:
            claimed[target] = source
            matched[source] = ColumnMatch(source, target, method, score, None)

    for column in source_columns:
        if column in exact_names:
            claim(column, column, "exact", 100.0)

    for column in source_columns:
        if column in matched:
            continue
        target = synonym_lookup.get(normalise(column))
        if target is not None:
            claim(column, target, "synonym", 100.0)

    if accepted:
        for column in source_columns:
            if column in matched:
                continue
            target = accepted.get(normalise(column))
            if target is not None:
                claim(column, target, "assist", 100.0)

    # Score every remaining column first, then claim best-first so the
    # strongest fuzzy match wins when two columns point at one field.
    fuzzy_candidates: list[tuple[float, str, str | None, str | None]] = []
    for column in source_columns:
        if column in matched:
            continue
        norm = normalise(column)
        best_score = 0.0
        best_target: str | None = None
        ambiguous_with: str | None = None
        for label, target in synonym_lookup.items():
            score = fuzz.ratio(norm, label)
            if score > best_score:
                best_score, best_target, ambiguous_with = score, target, None
            elif score == best_score and target != best_target:
                ambiguous_with = target
        fuzzy_candidates.append((best_score, column, best_target, ambiguous_with))

    for score, column, target, ambiguous_with in sorted(fuzzy_candidates, reverse=True):
        if target is None or score < threshold:
            matched[column] = ColumnMatch(
                column, None, None, None, f"no match at or above threshold {threshold:g}"
            )
        elif ambiguous_with is not None:
            matched[column] = ColumnMatch(
                column,
                None,
                None,
                None,
                f"ambiguous match: '{target}' and '{ambiguous_with}' both score {score:.0f}",
            )
        else:
            claim(column, target, "fuzzy", score)

    results = [matched[column] for column in source_columns]
    for match in results:
        if match.target:
            logger.debug(
                "mapped column=%r field=%s method=%s score=%.0f",
                match.source,
                match.target,
                match.method,
                match.score,
            )
        else:
            logger.debug("unmapped column=%r reason=%s", match.source, match.reason)
    return results
