"""Config generation from real files, PROPOSED markers and confirmation.

A generated configuration must never silently become the configuration of
record: every inference is marked, muster refuses to run while markers
remain, and 'muster confirm' (or hand-editing) is the only way into service.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from muster.cli import app
from muster.config import ConfigError, load_config
from muster.profiling import profile_folder
from muster.scaffold import confirm_text, propose_config

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures"


def _propose(folder: Path) -> str:
    return propose_config(profile_folder(folder), folder.as_posix())


def test_proposal_clusters_variants_and_infers_types(tmp_path):
    (tmp_path / "site_a.csv").write_text(
        "Receival ID,Grower,Net Tonnes,Delivered\nR-1,Marram Downs,32.4,2024-01-05\n"
    )
    (tmp_path / "site_b.csv").write_text(
        "receival id,Grower,Net tonnes,Delivered\nR-2,Karrilong Farms,28,06/01/2024\n"
    )
    text = _propose(tmp_path)

    # 'Receival ID' and 'receival id' cluster into one field; the earliest
    # most-common variant names it, and both headings survive as synonyms.
    assert "- name: receival_id" in text
    assert '"receival id"' in text
    # Mixed integer and float observations propose float.
    assert "type: float  # PROPOSED: mixed integer and float" in text
    # Present-and-non-empty everywhere proposes required.
    assert "required: true  # PROPOSED: present and non-empty in all 2 files" in text
    # Every inference line carries a marker.
    for line in text.splitlines():
        if line.strip().startswith(("- name:", "type:", "required:", "synonyms:")):
            assert "# PROPOSED" in line, line


def test_dissimilar_headings_stay_separate_proposals(tmp_path):
    (tmp_path / "a.csv").write_text("Customer Name,x\nAlice,1\n")
    (tmp_path / "b.csv").write_text("CUSTOMER_NUMBER,x\nC-1,2\n")
    text = _propose(tmp_path)
    # A wrong join could smuggle a bad synonym past review; these two score
    # under the cluster threshold and must stay separate fields.
    assert "- name: customer_name" in text
    assert "- name: customer_number" in text


def test_crafted_heading_cannot_inject_yaml_structure(tmp_path):
    evil = "end]\nfields: []\n# owned: {}"
    (tmp_path / "a.csv").write_text(f'"{evil}",ok\n1,2\n')
    text = _propose(tmp_path)
    confirmed, _ = confirm_text(text)
    config_path = tmp_path / "muster.yaml"
    config_path.write_text(confirmed, encoding="utf-8")
    config = load_config(config_path)
    # The crafted heading survives only as quoted data, never as structure.
    assert len(config.fields) == 2
    assert any(evil in spec.synonyms for spec in config.fields)


def test_unconfirmed_config_is_refused_and_confirm_accepts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sources = tmp_path / "sources"
    sources.mkdir()
    (sources / "north.csv").write_text("id,amount\nA-1,10.5\n")

    result = runner.invoke(app, ["init", "--from", "sources"])
    assert result.exit_code == 0, result.output
    assert "PROPOSED" in (tmp_path / "muster.yaml").read_text(encoding="utf-8")

    # The generated file is a proposal, not a configuration of record.
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "PROPOSED" in result.output

    result = runner.invoke(app, ["confirm"])
    assert result.exit_code == 0, result.output
    assert "configuration of record" in " ".join(result.output.split())
    text = (tmp_path / "muster.yaml").read_text(encoding="utf-8")
    assert "PROPOSED" not in text

    result = runner.invoke(app, ["run"])
    assert result.exit_code == 0, result.output
    assert "Published 1 of 1 row(s)" in result.output

    # Confirming twice is a no-op, not an error.
    result = runner.invoke(app, ["confirm"])
    assert result.exit_code == 0
    assert "nothing to confirm" in result.output


def test_proposal_from_the_shipped_fixtures_round_trips(tmp_path):
    text = _propose(FIXTURES)
    config_path = tmp_path / "muster.yaml"
    config_path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="PROPOSED"):
        load_config(config_path)
    confirmed, count = confirm_text(text)
    assert count > 0
    assert "PROPOSED" not in confirmed
    config_path.write_text(confirmed, encoding="utf-8")
    config = load_config(config_path)
    names = {spec.name for spec in config.fields}
    assert {"date_joined", "ltv", "full_name"} <= names


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "muster.yaml").write_text("fields: []\n")
    result = runner.invoke(app, ["init", "--from", "."])
    assert result.exit_code == 1
    assert "--force" in result.output
