"""Unit tests for type coercion and per-cell failure capture."""

import datetime

import polars as pl

from muster.coercion import coerce_series


def test_integer_coercion_handles_thousands_separators():
    coerced, failed = coerce_series(
        pl.Series("n", ["1,234", "56", "-7", "not a number"]), "integer"
    )
    assert coerced.to_list() == [1234, 56, -7, None]
    assert failed.to_list() == [False, False, False, True]


def test_float_coercion_strips_grouping_but_not_decimals():
    coerced, failed = coerce_series(
        pl.Series("v", ["1,204.50", "88.00", "n/a"]), "float"
    )
    assert coerced.to_list() == [1204.5, 88.0, None]
    assert failed.to_list() == [False, False, True]


def test_boolean_coercion_accepts_common_variants():
    coerced, failed = coerce_series(
        pl.Series("b", ["true", "No", "Y", "0", "maybe"]), "boolean"
    )
    assert coerced.to_list() == [True, False, True, False, None]
    assert failed.to_list() == [False, False, False, False, True]


def test_date_coercion_accepts_multiple_formats():
    coerced, failed = coerce_series(
        pl.Series("d", ["2023-04-12", "14/02/2023", "05 Mar 2024", "sometime in June"]),
        "date",
    )
    assert coerced.to_list() == [
        datetime.date(2023, 4, 12),
        datetime.date(2023, 2, 14),
        datetime.date(2024, 3, 5),
        None,
    ]
    assert failed.to_list() == [False, False, False, True]


def test_day_first_is_preferred_over_month_first():
    coerced, _ = coerce_series(pl.Series("d", ["05/03/2024", "03/25/2024"]), "date")
    assert coerced.to_list() == [
        datetime.date(2024, 3, 5),  # day-first reading
        datetime.date(2024, 3, 25),  # only a month-first reading is possible
    ]


def test_month_name_mixes_never_crash_the_parser():
    # Polars' vectorised %b/%B fast path panics on this exact value mix, so
    # month-name formats must go through the per-cell fallback. Untrusted
    # input becomes a failure record, never a crash.
    coerced, failed = coerce_series(
        pl.Series("d", ["05 Mar 2024", "17 Sep 2022", "sometime in June", "22 Dec 2023"]),
        "date",
    )
    assert coerced.to_list() == [
        datetime.date(2024, 3, 5),
        datetime.date(2022, 9, 17),
        None,
        datetime.date(2023, 12, 22),
    ]
    assert failed.to_list() == [False, False, True, False]


def test_datetime_coercion_reads_dates_as_midnight():
    coerced, failed = coerce_series(
        pl.Series("t", ["2024-01-02T03:04:05", "14/02/2023", "12 June 2023", "nope"]),
        "datetime",
    )
    assert coerced.to_list() == [
        datetime.datetime(2024, 1, 2, 3, 4, 5),
        datetime.datetime(2023, 2, 14),
        datetime.datetime(2023, 6, 12),
        None,
    ]
    assert failed.to_list() == [False, False, False, True]


def test_empty_cells_become_nulls_not_failures():
    coerced, failed = coerce_series(pl.Series("v", ["", "  ", None, "5"]), "integer")
    assert coerced.to_list() == [None, None, None, 5]
    assert failed.to_list() == [False, False, False, False]


def test_string_coercion_trims_and_nulls_empties():
    coerced, failed = coerce_series(pl.Series("s", ["  hi  ", ""]), "string")
    assert coerced.to_list() == ["hi", None]
    assert failed.to_list() == [False, False]
