"""The bundled demo: generated, run, and misbehaving exactly as designed."""

import json
import re

import polars as pl
from typer.testing import CliRunner

from muster.cli import app
from muster.manifest import verify_chain

runner = CliRunner()


def test_demo_generates_and_runs_the_full_pipeline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "Published 11 of 17 row(s) from 3 file(s); 5 held, 1 superseded." in output
    assert "misbehaves on purpose" in output

    demo = tmp_path / "demo"
    assert (demo / "sources" / "receivals_karrilong.csv").is_file()
    assert (demo / "sources" / "grain_intake_mundawarra.csv").is_file()
    assert (demo / "sources" / "bellandry_receivals.xlsx").is_file()
    assert (demo / "mapping-review.yaml").is_file()
    assert (demo / "muster.schedule").read_text(encoding="utf-8") == "0 6 * * 1-5\n"
    assert (demo / "warehouse.db").is_file()

    published = pl.read_csv(demo / "output" / "receivals.csv")
    ids = sorted(published.get_column("receival_id").to_list())
    # The conflicting ticket pair and the three broken rows are held; the
    # agreeing duplicate merged into one provenance-stamped row.
    assert "R-1004" not in ids and "R-2003" not in ids
    assert ids.count("R-1006") == 1
    merged = published.filter(pl.col("receival_id") == "R-1006")
    assert merged.get_column("_source_file")[0] == (
        "sources/receivals_karrilong.csv; sources/bellandry_receivals.xlsx"
    )

    exceptions = pl.read_csv(demo / "output" / "exceptions.csv")
    kinds = sorted(exceptions.get_column("kind").to_list())
    assert kinds == [
        "coercion",
        "coercion",
        "conflict",
        "duplicate_key",
        "rule_allowed_values",
        "rule_range",
        "rule_range",
        "unmapped_column",
    ]

    run_ids = verify_chain(demo / "runs")
    assert len(run_ids) == 2
    manifest = json.loads(
        (demo / "runs" / run_ids[0] / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["totals"] == {
        "rows_in": 17,
        "rows_published": 11,
        "rows_held": 5,
        "rows_superseded": 1,
        "rows_remediated": 0,
        "errors": 4,
        "warnings": 4,
    }
    assert (demo / "output" / "report.html").is_file()
    publish_manifest = json.loads(
        (demo / "runs" / run_ids[1] / "manifest.json").read_text(encoding="utf-8")
    )
    assert publish_manifest["kind"] == "publish"
    assert publish_manifest["publish"]["outcome"] == "published"
    assert publish_manifest["publish"]["forced"] is True

    # The promised remediation arc: the demo prints the exact command that
    # corrects the uncoercible weight on R-2004; running it and rerunning
    # publishes the row — held 5 -> 4, published 11 -> 12 — with the human
    # decision recorded in the manifest chain.
    match = re.search(r"muster resolve ([0-9a-f]{16})", output)
    assert match, "demo output should print a ready-to-paste resolve command"
    fingerprint = match.group(1)
    monkeypatch.chdir(demo)
    result = runner.invoke(
        app,
        [
            "resolve",
            fingerprint,
            "--set",
            "tonnes=27.9",
            "--note",
            "weighbridge docket shows 27.9 t",
        ],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 2, result.output  # other deliberate errors remain
    rerun = " ".join(result.output.split())
    assert "Published 12 of 17 row(s) from 3 file(s); 4 held, 1 superseded." in rerun
    assert "1 row(s) recovered via remediation" in rerun

    republished = pl.read_csv(demo / "output" / "receivals.csv")
    recovered = republished.filter(pl.col("receival_id") == "R-2004")
    assert recovered.height == 1
    assert recovered.get_column("tonnes")[0] == 27.9

    run_ids = verify_chain(demo / "runs")
    assert len(run_ids) == 3
    manifest = json.loads(
        (demo / "runs" / run_ids[-1] / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["totals"]["rows_remediated"] == 1
    assert manifest["remediation"]["resolutions"] == [fingerprint]
    monkeypatch.chdir(tmp_path)

    # The demo folder is also the promised playground for config generation.
    result = runner.invoke(
        app, ["init", "--from", "demo/sources", "--path", str(tmp_path / "proposed.yaml")]
    )
    assert result.exit_code == 0, result.output
    proposed = (tmp_path / "proposed.yaml").read_text(encoding="utf-8")
    assert "PROPOSED" in proposed
    assert "- name: receival_id" in proposed


def test_demo_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "demo").mkdir()
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 1
    assert "--force" in result.output
    result = runner.invoke(app, ["demo", "--force"])
    assert result.exit_code == 0, result.output
