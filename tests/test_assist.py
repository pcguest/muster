"""Assisted mapping: redaction, provider clients and the review workflow.

Every test runs with the HTTP transport mocked — no network, ever. The
tests pin the privacy stance: only column headings, inferred types and
redacted samples are sent, and nothing a model says is applied until a
person accepts it.
"""

import json
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

import muster.assist as assist_module
from muster.assist import (
    REVIEW_FILE_NAME,
    AssistError,
    AssistUnavailable,
    MappingProposal,
    ReviewFile,
    accepted_synonyms,
    build_evidence,
    load_review_file,
    propose_mappings,
    redact,
    write_review_file,
)
from muster.cli import app
from muster.config import AssistConfig, Config, RedactionConfig

runner = CliRunner()

FIELDS = [
    {"name": "quantity", "type": "float"},
    {"name": "site", "type": "string"},
]


def _config(**assist_overrides) -> Config:
    return Config.model_validate(
        {"fields": FIELDS, "assist": assist_overrides} if assist_overrides else {"fields": FIELDS}
    )


def _anthropic_reply(proposals) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(proposals)}]}


def test_redaction_masks_digits_and_truncates():
    redaction = RedactionConfig(mask_digits=True, truncate=10)
    assert redact("Invoice 2024-118", redaction) == "Invoice ##…"
    assert redact("  spaced  ", redaction) == "spaced"
    assert redact("no digits", RedactionConfig(mask_digits=False, truncate=4)) == "no d…"


def test_evidence_holds_only_headings_types_and_redacted_samples():
    samples = {"Qty (t)": ["1.5", "22.0", "1.5", "", "9", "8", "7", "6"]}
    evidence = build_evidence(samples, AssistConfig())
    assert evidence == [
        {
            "column": "Qty (t)",
            "inferred_type": "float",
            "samples": ["#.#", "##.#", "#", "#", "#"],  # capped at five, distinct
        }
    ]


def test_no_api_key_means_unavailable_not_broken(monkeypatch):
    monkeypatch.delenv("MUSTER_LLM_API_KEY", raising=False)
    with pytest.raises(AssistUnavailable, match="MUSTER_LLM_API_KEY"):
        propose_mappings({"Qty (t)": ["1.5"]}, _config())


def test_anthropic_request_shape_and_privacy(monkeypatch):
    monkeypatch.setenv("MUSTER_LLM_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers, body, timeout):
        captured.update(url=url, headers=headers, body=body)
        return _anthropic_reply(
            [{"column": "Qty (t)", "target": "quantity", "confidence": 88, "rationale": "mass"}]
        )

    monkeypatch.setattr(assist_module, "_post_json", fake_post)
    review = propose_mappings({"Qty (t)": ["1204.5", "88.0"]}, _config())

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "test-key"
    payload = json.dumps(captured["body"])
    # Raw cell values and file names never leave the machine.
    assert "1204.5" not in payload and "88.0" not in payload
    assert "####.#" in payload  # the redacted form travelled instead
    assert review.proposals == [
        MappingProposal(
            column="Qty (t)",
            target="quantity",
            confidence=88,
            rationale="mass",
            samples=["####.#", "##.#"],
            status="pending",
        )
    ]


def test_openai_compatible_request_shape(monkeypatch):
    monkeypatch.setenv("MUSTER_LLM_API_KEY", "test-key")
    captured = {}

    def fake_post(url, headers, body, timeout):
        captured.update(url=url, headers=headers)
        content = json.dumps(
            [{"column": "Qty (t)", "target": "quantity", "confidence": 70, "rationale": "r"}]
        )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(assist_module, "_post_json", fake_post)
    config = _config(provider="openai_compatible", base_url="https://llm.example/v1", model="m")
    review = propose_mappings({"Qty (t)": ["1"]}, config)
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert review.proposals[0].target == "quantity"


def test_untrusted_model_output_is_validated(monkeypatch):
    monkeypatch.setenv("MUSTER_LLM_API_KEY", "k")

    def fake_post(url, headers, body, timeout):
        return _anthropic_reply(
            [
                {"column": "Qty (t)", "target": "not_a_field", "confidence": 99},
                {"column": "never seen", "target": "quantity", "confidence": 99},
                "gibberish",
                {"column": "Site Ref", "target": "site", "confidence": 900, "rationale": "x" * 999},
            ]
        )

    monkeypatch.setattr(assist_module, "_post_json", fake_post)
    review = propose_mappings({"Qty (t)": ["1"], "Site Ref": ["a"]}, _config())
    # Unknown targets, unknown columns and non-objects are dropped;
    # confidence is clamped and rationale truncated.
    assert len(review.proposals) == 1
    proposal = review.proposals[0]
    assert (proposal.column, proposal.target) == ("Site Ref", "site")
    assert proposal.confidence == 100
    assert len(proposal.rationale) == 300


def test_fenced_reply_parses_and_garbage_raises(monkeypatch):
    monkeypatch.setenv("MUSTER_LLM_API_KEY", "k")
    replies = iter(
        [
            {"content": [{"type": "text", "text": '```json\n[{"column": "C", "target": "site", "confidence": 1}]\n```'}]},
            {"content": [{"type": "text", "text": "I could not decide."}]},
        ]
    )
    monkeypatch.setattr(assist_module, "_post_json", lambda *a, **k: next(replies))
    review = propose_mappings({"C": ["x"]}, _config())
    assert review.proposals[0].target == "site"
    with pytest.raises(AssistError, match="JSON array"):
        propose_mappings({"C": ["x"]}, _config())


def _stage_project(tmp_path) -> None:
    (tmp_path / "muster.yaml").write_text(
        "fields:\n"
        "  - name: quantity\n"
        "    type: float\n"
        "  - name: site\n"
        "    type: string\n"
        'sources: ["sources/*.csv"]\n',
        encoding="utf-8",
    )
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "a.csv").write_text("Qty (t),Loc Code\n1.5,K1\n2.0,M4\n", encoding="utf-8")


def test_run_assist_writes_review_and_acceptance_applies(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MUSTER_LLM_API_KEY", "k")
    _stage_project(tmp_path)

    def fake_post(url, headers, body, timeout):
        return _anthropic_reply(
            [
                {"column": "Qty (t)", "target": "quantity", "confidence": 91, "rationale": "tonnage"},
                {"column": "Loc Code", "target": None, "confidence": 30, "rationale": "unclear"},
            ]
        )

    monkeypatch.setattr(assist_module, "_post_json", fake_post)

    result = runner.invoke(app, ["run", "--assist"])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "no cell data, no file names" in output
    assert "2 proposal(s) written" in output
    review_path = tmp_path / REVIEW_FILE_NAME
    review = load_review_file(review_path)
    assert all(p.status == "pending" for p in review.proposals)

    # Nothing applied yet: the column is still unmapped on a plain run.
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    exceptions = pl.read_csv(tmp_path / "output" / "exceptions.csv")
    assert "Qty (t)" in exceptions.get_column("column").to_list()

    # Interactive review: accept the quantity mapping; the null-target
    # proposal is rejected without prompting.
    result = runner.invoke(app, ["review"], input="y\n")
    assert result.exit_code == 0, result.output
    review = load_review_file(review_path)
    statuses = {p.column: p.status for p in review.proposals}
    assert statuses == {"Qty (t)": "accepted", "Loc Code": "rejected"}

    # The accepted mapping applies from the next run.
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    consolidated = pl.read_csv(tmp_path / "output" / "consolidated.csv")
    assert consolidated.get_column("quantity").to_list() == [1.5, 2.0]
    exceptions = pl.read_csv(tmp_path / "output" / "exceptions.csv")
    assert "Qty (t)" not in exceptions.get_column("column").to_list()

    # Reviewing again finds nothing pending.
    result = runner.invoke(app, ["review"])
    assert result.exit_code == 0
    assert "No pending proposals" in result.output


def test_review_reject_all_keeps_columns_unmapped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _stage_project(tmp_path)
    review = ReviewFile(
        generated_at="2026-07-11T00:00:00+00:00",
        provider="anthropic",
        model="m",
        proposals=[
            MappingProposal(column="Qty (t)", target="quantity", confidence=91)
        ],
    )
    write_review_file(review, tmp_path / REVIEW_FILE_NAME)

    result = runner.invoke(app, ["review", "--reject-all"])
    assert result.exit_code == 0, result.output
    config = Config.model_validate({"fields": FIELDS})
    assert accepted_synonyms(tmp_path / REVIEW_FILE_NAME, config) == {}

    result = runner.invoke(app, ["run"])
    exceptions = pl.read_csv(tmp_path / "output" / "exceptions.csv")
    assert "Qty (t)" in exceptions.get_column("column").to_list()


def test_review_accept_all_and_stale_targets_are_ignored(tmp_path):
    review = ReviewFile(
        generated_at="2026-07-11T00:00:00+00:00",
        provider="anthropic",
        model="m",
        proposals=[
            MappingProposal(column="Qty (t)", target="quantity", status="accepted"),
            MappingProposal(column="Old", target="gone_field", status="accepted"),
            MappingProposal(column="Loc", target="site", status="rejected"),
        ],
    )
    path = tmp_path / REVIEW_FILE_NAME
    write_review_file(review, path)
    config = Config.model_validate({"fields": FIELDS})
    # Accepted + valid applies; stale targets and rejections never do.
    assert accepted_synonyms(path, config) == {"qty t": "quantity"}


def test_missing_review_file_is_no_assistance(tmp_path):
    config = Config.model_validate({"fields": FIELDS})
    assert accepted_synonyms(tmp_path / REVIEW_FILE_NAME, config) == {}
