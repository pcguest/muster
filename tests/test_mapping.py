"""Unit tests for column mapping."""

from muster.config import FieldSpec
from muster.mapping import map_columns, normalise

FIELDS = [
    FieldSpec(name="customer_id", synonyms=["client id", "customer number"]),
    FieldSpec(name="full_name", synonyms=["name", "customer name"]),
    FieldSpec(name="signup_date", synonyms=["date joined"]),
]


def by_source(matches):
    return {m.source: m for m in matches}


def test_normalise_strips_case_and_punctuation():
    assert normalise("  Sign-Up__Date! ") == "sign up date"


def test_exact_match_wins():
    matches = by_source(map_columns(["customer_id"], FIELDS, 90))
    match = matches["customer_id"]
    assert (match.target, match.method) == ("customer_id", "exact")


def test_synonym_match_is_case_and_punctuation_insensitive():
    matches = by_source(map_columns(["Client-ID", "Date Joined"], FIELDS, 90))
    assert matches["Client-ID"].target == "customer_id"
    assert matches["Client-ID"].method == "synonym"
    assert matches["Date Joined"].target == "signup_date"


def test_fuzzy_match_above_threshold():
    matches = by_source(map_columns(["FullName"], FIELDS, 90))
    match = matches["FullName"]
    assert match.target == "full_name"
    assert match.method == "fuzzy"
    assert match.score >= 90


def test_fuzzy_below_threshold_is_unmapped_with_reason():
    matches = by_source(map_columns(["Notes"], FIELDS, 90))
    match = matches["Notes"]
    assert match.target is None
    assert "threshold" in match.reason


def test_duplicate_target_is_not_claimed_twice():
    matches = by_source(map_columns(["customer_id", "Client ID"], FIELDS, 90))
    assert matches["customer_id"].target == "customer_id"
    duplicate = matches["Client ID"]
    assert duplicate.target is None
    assert "already mapped" in duplicate.reason


def test_ambiguous_fuzzy_tie_is_unmapped():
    fields = [
        FieldSpec(name="alpha_code"),
        FieldSpec(name="alpha_node"),
    ]
    matches = by_source(map_columns(["alpha_bode"], fields, 50))
    match = matches["alpha_bode"]
    assert match.target is None
    assert "ambiguous" in match.reason
