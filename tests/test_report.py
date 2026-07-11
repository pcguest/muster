"""Smoke tests for the HTML run report."""

import polars as pl

from muster.config import Config
from muster.mapping import ColumnMatch
from muster.records import ExceptionRecord
from muster.report import RunReportData, build_report, render_report, write_report


def _data() -> RunReportData:
    config = Config.model_validate(
        {
            "fields": [
                {"name": "id", "type": "string", "required": True},
                {"name": "amount", "type": "float"},
            ],
            "validation": {"keys": ["id"]},
        }
    )
    published = pl.DataFrame(
        {
            "_source_file": ["a.csv", "b.csv"],
            "_row": [2, 2],
            "id": ["K1", "K2"],
            "amount": [10.0, None],
        }
    )
    exceptions = [
        ExceptionRecord(
            file="a.csv", row=3, column="Amount <b>Due</b>", value="n/a",
            kind="coercion", reason="cannot coerce to float for field 'amount'",
        ),
        ExceptionRecord(
            file="a.csv, b.csv", column="amount",
            value="a.csv row 4: 10.0 | b.csv row 4: <script>alert(1)</script>",
            kind="conflict", severity="error",
            reason="conflicting values for key 'K9'; no survivorship strategy configured; rows held for review",
        ),
        ExceptionRecord(
            file="b.csv", column="Notes", kind="unmapped_column",
            severity="warning", reason="unmapped column: no match",
        ),
    ]
    mappings = [
        ("a.csv", ColumnMatch("id", "id", "exact", 100.0, None)),
        ("a.csv", ColumnMatch("Amount <b>Due</b>", "amount", "fuzzy", 92.0, None)),
        ("b.csv", ColumnMatch("Notes", None, None, None, "no match")),
    ]
    return build_report(
        config=config,
        published=published,
        file_rows={"a.csv": 4, "b.csv": 3},
        mappings=mappings,
        exceptions=exceptions,
        run_id="20260711T090000Z",
        generated_at="2026-07-11T09:00:00+00:00",
        config_file="muster.yaml",
        config_sha256="ab" * 32,
        duration_seconds=0.42,
        rows_in=7,
        rows_published=2,
        rows_held=3,
        rows_superseded=0,
        conflicts_held=1,
    )


def test_report_metrics_and_key_strings():
    data = _data()
    assert data.error_count == 2
    assert data.warning_count == 1
    amount = next(f for f in data.fields if f.name == "amount")
    assert amount.completeness_pct == 50.0
    # one violating row (a.csv row 3, via the mapped source column) of 7 in
    assert amount.validity_pct == 85.7
    a_csv = next(s for s in data.sources if s.file == "a.csv")
    assert a_csv.errors == 1
    assert a_csv.score == 75
    assert [c.column for c in data.conflicts] == ["amount"]

    html = render_report(data)
    for expected in (
        "Muster run report",
        "20260711T090000Z",
        "rows published",
        "Conflicts held for review",
        "conflicting values for key &#x27;K9&#x27;",
        "Mapping decisions",
        "fuzzy 92",
        "unmapped",
    ):
        assert expected in html


def test_report_escapes_untrusted_values_and_headings():
    html = render_report(_data())
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>Due</b>" not in html
    assert "Amount &lt;b&gt;Due&lt;/b&gt;" in html


def test_report_is_self_contained():
    html = render_report(_data())
    for banned in ("http://", "https://", "<script", "src=", "@import"):
        assert banned not in html


def test_report_round_trips_through_json_and_writes(tmp_path):
    data = _data()
    restored = RunReportData.from_json(data.to_json())
    assert restored == data
    html_path = tmp_path / "report.html"
    data_path = tmp_path / "report.json"
    write_report(data, html_path, data_path)
    assert html_path.is_file()
    assert "Muster run report" in html_path.read_text(encoding="utf-8")
    assert RunReportData.from_json(data_path.read_text(encoding="utf-8")) == data
