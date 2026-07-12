"""Optional LLM assistance for columns fuzzy matching cannot map.

Privacy stance, stated plainly: no cell data leaves the machine. A request
carries only column headings, an inferred type per column, and up to five
sample values that have been redacted first (digits masked, length
truncated — both configurable). File names are not sent either. The exact
samples sent are recorded in the review file so the user can see precisely
what left the machine.

Nothing the model says is ever applied on its own. Proposals are written to
``mapping-review.yaml`` with confidence and rationale, and sit there as
``pending`` until a person accepts or rejects each one — interactively with
``muster review``, or by editing the file. Only accepted proposals feed the
mapping stage of later runs.

Without a MUSTER_LLM_API_KEY environment variable the feature is simply
unavailable and Muster works exactly as before. The key is read from the
environment only; it never appears in configuration or on disk.

The client is provider-agnostic: Anthropic's Messages API or any
OpenAI-compatible chat-completions endpoint, selected in ``muster.yaml``.
Model output is untrusted input — it is parsed as JSON, validated against
the declared schema, and anything malformed or referring to unknown fields
is dropped with a log line, never applied.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from muster.config import AssistConfig, Config, RedactionConfig
from muster.mapping import normalise
from muster.profiling import infer_type

logger = logging.getLogger(__name__)

API_KEY_ENV = "MUSTER_LLM_API_KEY"
REVIEW_FILE_NAME = "mapping-review.yaml"

_REVIEW_HEADER = """\
# Muster assisted-mapping proposals — a person decides, this file records.
#
# Each proposal below came from an LLM shown only column headings, inferred
# types and the redacted samples listed here (exactly what left the machine;
# no cell data, no file names). Nothing is applied while a proposal is
# 'pending': accept or reject each one with 'muster review', or edit the
# status fields by hand. Accepted proposals are honoured by later runs.
"""


class AssistError(RuntimeError):
    """Raised when assistance was requested but could not be provided."""


class AssistUnavailable(AssistError):
    """Raised when no API key is configured; the tool works fine without."""


class MappingProposal(BaseModel):
    """One model-proposed mapping, plus the human decision about it."""

    column: str
    target: str | None = None
    confidence: float = Field(default=0, ge=0, le=100)
    rationale: str = ""
    samples: list[str] = Field(default_factory=list)
    status: Literal["pending", "accepted", "rejected"] = "pending"


class ReviewFile(BaseModel):
    """The on-disk review document holding proposals and decisions."""

    generated_at: str
    provider: str
    model: str
    proposals: list[MappingProposal]


def redact(value: str, redaction: RedactionConfig) -> str:
    """Redact one sample value before it may leave the machine."""
    redacted = value.strip()
    if redaction.mask_digits:
        redacted = re.sub(r"\d", "#", redacted)
    if len(redacted) > redaction.truncate:
        redacted = redacted[: redaction.truncate] + "…"
    return redacted


def build_evidence(
    unmapped_samples: Mapping[str, Sequence[str]], assist: AssistConfig
) -> list[dict[str, Any]]:
    """Redacted, sendable evidence for each unmapped column heading."""
    evidence = []
    for column, raw_samples in sorted(unmapped_samples.items()):
        distinct: list[str] = []
        for value in raw_samples:
            if value and value not in distinct:
                distinct.append(value)
        samples = [redact(v, assist.redaction) for v in distinct[: assist.max_samples]]
        evidence.append(
            {
                "column": column,
                "inferred_type": infer_type(list(distinct)),
                "samples": samples,
            }
        )
    return evidence


def _prompt(evidence: list[dict[str, Any]], config: Config) -> str:
    fields = [
        {"name": spec.name, "type": spec.type, "synonyms": spec.synonyms}
        for spec in config.fields
    ]
    return (
        "You map spreadsheet column headings onto a canonical schema.\n"
        "Canonical fields:\n"
        f"{json.dumps(fields, ensure_ascii=False, indent=2)}\n\n"
        "Unmapped columns (sample values are redacted):\n"
        f"{json.dumps(evidence, ensure_ascii=False, indent=2)}\n\n"
        "For each unmapped column, propose the canonical field it holds, or "
        "null if none fits. Reply with ONLY a JSON array, one object per "
        'column: {"column": <heading>, "target": <field name or null>, '
        '"confidence": <0-100>, "rationale": <one short sentence>}.'
    )


def _post_json(
    url: str, headers: dict[str, str], body: dict[str, Any], timeout: int
) -> dict[str, Any]:
    """POST a JSON body and return the JSON response. Patched in tests."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    # The base URL is constrained to http(s) by AssistConfig's validator.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        reply = json.loads(response.read().decode("utf-8"))
    if not isinstance(reply, dict):
        raise AssistError("the provider reply was not a JSON object")
    return reply


def _request_completion(prompt: str, assist: AssistConfig, api_key: str) -> str:
    base = assist.resolved_base_url()
    if assist.provider == "anthropic":
        payload = _post_json(
            f"{base}/v1/messages",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            {
                "model": assist.model,
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            },
            assist.timeout_seconds,
        )
        blocks = payload.get("content", [])
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
    payload = _post_json(
        f"{base}/chat/completions",
        {"Authorization": f"Bearer {api_key}"},
        {
            "model": assist.model,
            "messages": [{"role": "user", "content": prompt}],
        },
        assist.timeout_seconds,
    )
    choices = payload.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "") or ""


def _extract_json_array(text: str) -> list[Any]:
    """Pull the JSON array out of a model reply; the reply is untrusted."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?|```$", "", stripped).strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("["), stripped.rfind("]")
        if start == -1 or end <= start:
            raise AssistError(
                "the model reply held no JSON array of proposals"
            ) from None
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AssistError(f"could not parse the model reply as JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise AssistError("the model reply was not a JSON array of proposals")
    return parsed


def _validate_proposals(
    raw: list[Any], evidence: list[dict[str, Any]], config: Config
) -> list[MappingProposal]:
    known_columns = {e["column"]: e for e in evidence}
    known_fields = {spec.name for spec in config.fields}
    proposals: list[MappingProposal] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            logger.warning("dropped non-object proposal from model reply")
            continue
        column = item.get("column")
        if not isinstance(column, str) or column not in known_columns or column in seen:
            logger.warning("dropped proposal for unknown or repeated column %r", column)
            continue
        target = item.get("target")
        if target is not None and (
            not isinstance(target, str) or target not in known_fields
        ):
            logger.warning(
                "dropped proposal mapping %r to unknown field %r", column, target
            )
            continue
        try:
            proposal = MappingProposal(
                column=column,
                target=target,
                confidence=min(100, max(0, float(item.get("confidence", 0)))),
                rationale=str(item.get("rationale", ""))[:300],
                samples=known_columns[column]["samples"],
            )
        except (ValidationError, TypeError, ValueError):
            logger.warning("dropped malformed proposal for column %r", column)
            continue
        seen.add(column)
        proposals.append(proposal)
    return proposals


def propose_mappings(
    unmapped_samples: Mapping[str, Sequence[str]], config: Config
) -> ReviewFile:
    """Ask the configured model to propose mappings for unmapped columns.

    Raises :class:`AssistUnavailable` when no API key is set and
    :class:`AssistError` when the request or reply cannot be used.
    """
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise AssistUnavailable(
            f"assist is unavailable: set the {API_KEY_ENV} environment variable "
            "to enable it; muster works fully without it"
        )
    evidence = build_evidence(unmapped_samples, config.assist)
    prompt = _prompt(evidence, config)
    try:
        reply = _request_completion(prompt, config.assist, api_key)
    except AssistError:
        raise
    except Exception as exc:
        raise AssistError(f"assist request failed: {exc}") from exc
    proposals = _validate_proposals(_extract_json_array(reply), evidence, config)
    logger.info(
        "assist proposed=%d of unmapped=%d provider=%s model=%s",
        len(proposals),
        len(evidence),
        config.assist.provider,
        config.assist.model,
    )
    return ReviewFile(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provider=config.assist.provider,
        model=config.assist.model,
        proposals=proposals,
    )


def write_review_file(review: ReviewFile, path: Path) -> None:
    body = yaml.safe_dump(
        review.model_dump(), sort_keys=False, allow_unicode=True, width=88
    )
    path.write_text(_REVIEW_HEADER + body, encoding="utf-8")


def load_review_file(path: Path) -> ReviewFile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AssistError(f"could not parse {path}: {exc}") from exc
    try:
        return ReviewFile.model_validate(raw)
    except ValidationError as exc:
        raise AssistError(f"invalid review file {path}:\n{exc}") from exc


def accepted_synonyms(path: Path, config: Config) -> dict[str, str]:
    """Human-accepted mappings, as normalised heading -> canonical field.

    Missing file means no assistance has been accepted: empty mapping.
    Accepted proposals whose target is no longer a declared field are
    ignored with a log line rather than misapplied.
    """
    if not path.is_file():
        return {}
    review = load_review_file(path)
    known = {spec.name for spec in config.fields}
    accepted: dict[str, str] = {}
    for proposal in review.proposals:
        if proposal.status != "accepted" or proposal.target is None:
            continue
        if proposal.target not in known:
            logger.warning(
                "ignoring accepted mapping %r -> %r: not a declared field",
                proposal.column,
                proposal.target,
            )
            continue
        accepted[normalise(proposal.column)] = proposal.target
    return accepted
