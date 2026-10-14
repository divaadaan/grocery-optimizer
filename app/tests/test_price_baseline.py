"""Unit tests for the self-derived price baseline.

The two tests that matter most are the ones asserting what the baseline
*refuses* to do: return a number on day 1, and call its median a regular price.
Both of those failures would silently restore the dishonest savings metric this
module exists to replace. No DB — ``baseline_from_rows`` is the pure half.
"""
from datetime import date, timedelta

from app.services.price_baseline import (
    DEFAULT_WINDOW_DAYS,
    MIN_DISTINCT_WEEKS,
    SaleBaseline,
    baseline_from_rows,
    discount_vs_typical_sale,
)


def _row(observed_on, sale_price, price_basis="sale_only", price=None):
    return {
        "observed_on": observed_on,
        "sale_price": sale_price,
        "price": price,
        "price_basis": price_basis,
    }


def _weekly(prices, start=date(2026, 9, 2), basis="sale_only"):
    """One observation per week, which is what weekly flyers actually give us."""
    return [
        _row(start + timedelta(weeks=i), price, basis)
        for i, price in enumerate(prices)
    ]


# --- limit (a): there is no baseline on day 1, and no fake one --------------

def test_no_history_at_all_returns_none():
    assert baseline_from_rows([], product_name="CHICKEN BREAST") is None


def test_day_one_returns_none_not_the_single_price():
    """A median of one observation is that observation.

    Returning it would make today's advertised price the historical norm and
    report ~$0 savings forever. None means "unknown" and callers must say so.
    """
    rows = [_row(date(2026, 10, 14), 4.99)]
    assert baseline_from_rows(rows, product_name="CHICKEN BREAST") is None


def test_three_weeks_is_still_not_enough():
    rows = _weekly([4.99, 5.49, 4.79])
    assert len(rows) == MIN_DISTINCT_WEEKS - 1
    assert baseline_from_rows(rows, product_name="CHICKEN BREAST") is None


def test_four_distinct_weeks_is_the_threshold():
    rows = _weekly([4.99, 5.49, 4.79, 5.99])
    baseline = baseline_from_rows(rows, product_name="CHICKEN BREAST")
    assert baseline is not None
    assert baseline.distinct_weeks == 4
    assert baseline.observed_sale_median == 5.24  # median of the four


def test_many_observations_in_one_week_do_not_substitute_for_history():
    """Re-ingesting the same weekly flyer daily is one week of information.

    This is the gate's whole point: sample_count is easy to inflate, so the
    threshold is on distinct weeks instead.
    """
    rows = [_row(date(2026, 10, 12) + timedelta(days=i), 4.99) for i in range(5)]
    assert len({r["observed_on"].isocalendar()[1] for r in rows}) == 1
    assert len(rows) > MIN_DISTINCT_WEEKS
    assert baseline_from_rows(rows, product_name="CHICKEN BREAST") is None


# --- the median itself ------------------------------------------------------

def test_median_not_mean_so_one_loss_leader_does_not_move_it():
    normal = _weekly([5.00, 5.00, 5.00, 5.00, 5.00])
    with_outlier = _weekly([5.00, 5.00, 5.00, 5.00, 0.99])

    a = baseline_from_rows(normal, product_name="x")
    b = baseline_from_rows(with_outlier, product_name="x")
    assert a.observed_sale_median == 5.00
    assert b.observed_sale_median == 5.00  # a mean would be 4.20


def test_window_and_sample_metadata_are_reported():
    rows = _weekly([4.00, 5.00, 6.00, 7.00], start=date(2026, 9, 2))
    baseline = baseline_from_rows(rows, product_name="CHICKEN BREAST")
    assert baseline.sample_count == 4
    assert baseline.distinct_weeks == 4
    assert baseline.window_days == DEFAULT_WINDOW_DAYS
    assert baseline.first_observed == date(2026, 9, 2)
    assert baseline.last_observed == date(2026, 9, 23)
    assert baseline.product_name == "CHICKEN BREAST"


def test_rows_with_null_sale_price_are_ignored():
    rows = _weekly([4.99, 5.49, 4.79, 5.99]) + [_row(date(2026, 10, 7), None)]
    baseline = baseline_from_rows(rows, product_name="x")
    assert baseline.sample_count == 4


def test_rows_with_no_observed_on_are_ignored():
    rows = _weekly([4.99, 5.49, 4.79, 5.99]) + [
        {"sale_price": 1.0, "observed_on": None, "price_basis": "sale_only"}
    ]
    baseline = baseline_from_rows(rows, product_name="x")
    assert baseline.sample_count == 4


# --- limit (b): this measures sale prices, not shelf prices -----------------

def test_flyer_only_history_is_flagged_as_sale_prices_only():
    """The honesty flag. Flyers only print items that are ON promotion, so the
    median is a typical *advertised* price and sits below the true shelf price.
    Savings computed against it is an understatement, by construction."""
    baseline = baseline_from_rows(
        _weekly([4.99, 5.49, 4.79, 5.99], basis="sale_only"), product_name="x"
    )
    assert baseline.shelf_price_samples == 0
    assert baseline.measures_sale_prices_only is True


def test_shelf_price_samples_counted_when_the_source_supplied_them():
    rows = (
        _weekly([4.99, 5.49], start=date(2026, 9, 2), basis="sale_only")
        + _weekly([4.79, 5.99], start=date(2026, 9, 16), basis="regular_and_sale")
    )
    baseline = baseline_from_rows(rows, product_name="x")
    assert baseline.shelf_price_samples == 2
    assert baseline.measures_sale_prices_only is False


def test_the_metric_is_not_named_like_a_regular_price():
    """Guards the naming decision, which is the whole point of the module.

    Renaming observed_sale_median to anything implying a shelf price, or
    discount_vs_typical_sale to estimated_savings, re-introduces the claim that
    flyer history measures regular prices. It does not.
    """
    fields = set(SaleBaseline.__dataclass_fields__)
    assert "observed_sale_median" in fields
    assert not {"regular_price", "estimated_savings", "savings"} & fields


# --- the delta --------------------------------------------------------------

def test_discount_vs_typical_sale_is_none_when_there_is_no_baseline():
    # Callers must treat this as unknown, never as zero savings.
    assert discount_vs_typical_sale(4.99, None) is None


def test_discount_vs_typical_sale_measures_against_the_median():
    baseline = baseline_from_rows(_weekly([5.00, 5.00, 5.00, 5.00]), product_name="x")
    assert discount_vs_typical_sale(4.00, baseline) == 1.00


def test_offer_priced_above_the_typical_sale_returns_a_negative():
    """Not clamped to 0: "this week's 'deal' is worse than usual" is real info."""
    baseline = baseline_from_rows(_weekly([5.00, 5.00, 5.00, 5.00]), product_name="x")
    assert discount_vs_typical_sale(6.50, baseline) == -1.50
