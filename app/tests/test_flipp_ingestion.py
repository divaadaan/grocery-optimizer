"""Unit tests for Flipp deal ingestion — normalisation and the provider seam.

No network. Every assertion runs against
``app/tests/fixtures/flipp_search_chicken_breast.json``, which is a verbatim
subset of a real response recorded live on 2026-10-14 (postal_code=M5V3A8,
q="chicken breast"): the top-level envelope is untouched, 12 of the 35 flyer
items are kept field-for-field, and only the multi-kilobyte signed image URLs
were elided. The items were chosen because they cover the awkward real cases —
a null ``name``, a null ``current_price``, a merchant name with a stray tab, an
integer price, and the 2-in-12 minority that carry ``original_price``.
"""
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.services.flipp_ingestion import (
    BASIS_REGULAR_AND_SALE,
    BASIS_SALE_ONLY,
    IngestionReport,
    NormalizedDeal,
    collect_deals,
    dedupe,
    normalize_item,
    normalize_items,
)

FIXTURE = Path(__file__).parent / "fixtures" / "flipp_search_chicken_breast.json"


@pytest.fixture(scope="module")
def recorded():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def raw_items(recorded):
    return recorded["items"]


class FakeProvider:
    """Stands in for FlippSearchClient. The ingestion seam, exercised."""

    source = "flipp"

    def __init__(self, by_term, fail_terms=()):
        self.by_term = by_term
        self.fail_terms = set(fail_terms)
        self.calls = []

    def search(self, term, postal_code):
        self.calls.append((term, postal_code))
        if term in self.fail_terms:
            from app.services.flipp_client import DealProviderError

            raise DealProviderError(f"boom for {term}")
        return self.by_term.get(term, [])


def _by_name(deals, name):
    match = [d for d in deals if d.product_name == name]
    assert len(match) == 1, f"expected one deal named {name!r}, got {match}"
    return match[0]


# --- the recorded response still has the shape we built against -------------

def test_fixture_envelope_matches_the_observed_response(recorded):
    # If the provider changes shape, this is the test that should go red first.
    assert "items" in recorded and isinstance(recorded["items"], list)
    assert "ecom_items" in recorded
    for item in recorded["items"]:
        assert item["item_type"] == "flyer"
        assert "flyer_item_id" in item
        assert "merchant_name" in item
        assert "valid_from" in item and "valid_to" in item


def test_regular_price_is_the_minority_case(raw_items):
    """The headline fact about this source: original_price is usually absent.

    Observed live: 5/35. If this ever flips, estimated_savings becomes viable
    again and app/services/price_baseline.py stops being load-bearing.
    """
    with_regular = [i for i in raw_items if i.get("original_price") is not None]
    assert len(with_regular) < len(raw_items) / 2


# --- normalisation ----------------------------------------------------------

def test_normalizes_a_full_item(raw_items):
    deals = normalize_items(raw_items, search_term="chicken breast")
    deal = _by_name(deals, "Boneless, Skinless Chicken Breasts")

    assert deal.merchant_name == "M&M Food Market"
    assert deal.sale_price == 29.99
    assert deal.regular_price == 39.99
    assert deal.source_item_id == "1045420746"
    assert deal.valid_from == date(2026, 10, 8)
    assert deal.valid_until == date(2026, 10, 15)
    assert deal.category == "Meat & Poultry"  # derived from the search term
    assert deal.search_term == "chicken breast"


def test_category_comes_from_the_search_term_not_the_payload(raw_items):
    # Every grocery item's _L1/_L2 is "Food, Beverages & Tobacco"/"Food Items",
    # which is useless, so the term that found it supplies the category.
    deals = normalize_items(raw_items, search_term="chicken breast")
    assert {d.category for d in deals} == {"Meat & Poultry"}

    unknown = normalize_items(raw_items, search_term="dragon fruit")
    assert {d.category for d in unknown} == {None}


def test_item_with_null_name_is_skipped(raw_items):
    # The fixture keeps the real image-only placement whose name is null.
    assert any(not i.get("name") for i in raw_items), "fixture lost its null-name item"

    report = IngestionReport(postal_code="M5V3A8", source="flipp")
    deals = normalize_items(raw_items, search_term="chicken breast", report=report)
    assert report.skipped_no_name >= 1
    assert all(d.product_name for d in deals)


def test_item_with_null_current_price_is_skipped(raw_items):
    report = IngestionReport(postal_code="M5V3A8", source="flipp")
    deals = normalize_items(raw_items, search_term="chicken breast", report=report)
    assert report.skipped_no_price >= 1
    assert all(d.sale_price > 0 for d in deals)


def test_merchant_name_whitespace_is_stripped(raw_items):
    # Observed live: "Cataldi\t".
    deals = normalize_items(raw_items, search_term="chicken breast")
    merchants = {d.merchant_name for d in deals}
    assert "Cataldi" in merchants
    assert all(m == m.strip() for m in merchants)


def test_integer_current_price_becomes_float(raw_items):
    # current_price arrives as int OR float: 13 and 29.99 in the same response.
    deals = normalize_items(raw_items, search_term="chicken breast")
    deal = _by_name(deals, "PC® BLUE MENU® CHICKEN BREASTS, 3'S")
    assert isinstance(deal.sale_price, float)
    assert deal.sale_price == 13.0


def test_unit_is_normalised_from_post_price_text(raw_items):
    deals = normalize_items(raw_items, search_term="chicken breast")
    # "LB", "lb", "/lb" all collapse to "lb"
    units = {d.unit for d in deals if d.unit}
    assert units <= {"lb"}, units
    assert "lb" in units


def test_price_basis_records_which_price_we_actually_saw(raw_items):
    deals = normalize_items(raw_items, search_term="chicken breast")
    with_regular = [d for d in deals if d.regular_price is not None]
    without = [d for d in deals if d.regular_price is None]

    assert with_regular and without, "fixture should cover both bases"
    assert all(d.price_basis == BASIS_REGULAR_AND_SALE for d in with_regular)
    assert all(d.price_basis == BASIS_SALE_ONLY for d in without)


def test_discount_is_none_not_zero_without_a_regular_price():
    """0 would read as 'no discount' and pollute idx_deals_discount ordering."""
    deal = NormalizedDeal(
        product_name="CHICKEN BREAST", merchant_name="No Frills", merchant_id="2332",
        sale_price=4.88, regular_price=None, unit="lb", category="Meat & Poultry",
        valid_from=date(2026, 10, 15), valid_until=date(2026, 10, 22),
        source_item_id="1", search_term="chicken breast",
    )
    assert deal.discount_percentage is None


def test_discount_computed_when_regular_price_present():
    deal = NormalizedDeal(
        product_name="x", merchant_name="m", merchant_id="1",
        sale_price=29.99, regular_price=39.99, unit=None, category=None,
        valid_from=date(2026, 10, 8), valid_until=date(2026, 10, 15),
        source_item_id="1", search_term="t",
    )
    assert deal.discount_percentage == 25


def test_regular_price_at_or_below_sale_price_is_discarded():
    """"Regular" <= sale is not a regular price; recording it would fake savings."""
    raw = {
        "name": "Milk", "current_price": 4.99, "original_price": 4.99,
        "merchant_name": "Metro", "merchant_id": 1, "flyer_item_id": 9,
        "valid_from": "2026-10-08T04:00:00+00:00",
        "valid_to": "2026-10-15T03:59:59+00:00",
    }
    deal = normalize_item(raw, search_term="milk")
    assert deal.regular_price is None
    assert deal.price_basis == BASIS_SALE_ONLY
    assert deal.discount_percentage is None


def test_non_food_items_are_dropped():
    # A "rice" search can return a rice cooker.
    raw = {
        "_L1": "Kitchen & Dining", "name": "Rice Cooker", "current_price": 39.99,
        "merchant_name": "Walmart", "merchant_id": 234, "flyer_item_id": 7,
        "valid_from": "2026-10-08T04:00:00+00:00",
        "valid_to": "2026-10-15T03:59:59+00:00",
    }
    assert normalize_item(raw, search_term="rice") == "non_food"


def test_missing_l1_is_kept_not_dropped():
    # Plenty of real flyer items carry no taxonomy; failing closed costs coverage.
    raw = {
        "name": "Long Grain Rice 8kg", "current_price": 12.99,
        "merchant_name": "Walmart", "merchant_id": 234, "flyer_item_id": 8,
        "valid_from": "2026-10-08T04:00:00+00:00",
        "valid_to": "2026-10-15T03:59:59+00:00",
    }
    deal = normalize_item(raw, search_term="rice")
    assert isinstance(deal, NormalizedDeal)
    assert deal.category == "Pantry"


def test_brand_is_never_guessed(raw_items):
    # The payload carries opaque brand_ids and no brand name, so deals.brand
    # stays NULL rather than holding something parsed out of the product name.
    deals = normalize_items(raw_items, search_term="chicken breast")
    assert not any(hasattr(d, "brand") and getattr(d, "brand", None) for d in deals)


def test_unparseable_dates_are_skipped():
    raw = {
        "name": "Chicken", "current_price": 5.0, "merchant_name": "Metro",
        "merchant_id": 1, "flyer_item_id": 10,
        "valid_from": "not-a-date", "valid_to": "2026-10-15T03:59:59+00:00",
    }
    assert normalize_item(raw, search_term="chicken breast") == "bad_dates"


# --- idempotency within a run -----------------------------------------------

def test_same_flyer_item_from_two_terms_collapses(raw_items):
    """Overlapping staples genuinely collide; the natural key must absorb it."""
    first = normalize_items(raw_items, search_term="chicken breast")
    second = normalize_items(raw_items, search_term="chicken thighs")

    unique, same_item, same_offer = dedupe(first + second)
    assert same_item == len(first)
    assert same_offer == 0
    assert len(unique) == len(first)
    # First term wins, so the derived category is stable in term order.
    assert {d.search_term for d in unique} == {"chicken breast"}


def test_dedupe_keeps_distinct_items(raw_items):
    deals = normalize_items(raw_items, search_term="chicken breast")
    unique, same_item, same_offer = dedupe(deals)
    assert (same_item, same_offer) == (0, 0)
    assert len(unique) == len(deals)


def test_same_product_name_at_two_merchants_is_two_deals(raw_items):
    """Two stores advertising "CHICKEN BREAST" are two offers, not a duplicate."""
    deals = normalize_items(raw_items, search_term="chicken breast")
    same_name = [d for d in deals if d.product_name == "CHICKEN BREAST"]
    assert len(same_name) == 2
    assert {d.merchant_name for d in same_name} == {
        "Real Canadian Superstore", "No Frills"
    }
    unique, same_item, same_offer = dedupe(deals)
    assert (same_item, same_offer) == (0, 0)


# --- the same offer printed twice in one flyer ------------------------------

def _offer(item_id, price=11.99, name="PRIME FRESH BONELESS", merchant="Food Basics"):
    return NormalizedDeal(
        product_name=name, merchant_name=merchant, merchant_id="2338",
        sale_price=price, regular_price=None, unit="lb", category="Meat & Poultry",
        valid_from=date(2026, 10, 8), valid_until=date(2026, 10, 15),
        source_item_id=str(item_id), search_term="chicken breast",
    )


def test_identical_offer_under_two_item_ids_collapses():
    """Observed live: Food Basics printed the same $11.99/lb offer as flyer
    items 1045444708 and 1045444966. Two placements, one offer. Left in, it
    would double-weight that product-day in the price baseline."""
    unique, same_item, same_offer = dedupe([_offer(1045444966), _offer(1045444708)])
    assert same_item == 0
    assert same_offer == 1
    assert len(unique) == 1


def test_duplicate_offer_representative_is_stable_across_runs():
    """The representative must not depend on provider result order.

    source_item_id is part of the deals natural key, so picking a different
    placement on a later run would INSERT a second row instead of updating the
    first — idempotency would fail silently.
    """
    run_a, _, _ = dedupe([_offer(1045444966), _offer(1045444708)])
    run_b, _, _ = dedupe([_offer(1045444708), _offer(1045444966)])
    assert run_a[0].source_item_id == run_b[0].source_item_id == "1045444708"


def test_same_product_at_different_prices_is_two_offers():
    """Probably two sizes. Not a duplicate — keep both."""
    unique, _, same_offer = dedupe([_offer(1, price=11.99), _offer(2, price=7.99)])
    assert same_offer == 0
    assert len(unique) == 2


def test_same_product_in_a_different_flyer_week_is_two_offers():
    """Next week's flyer must accumulate history, not overwrite it."""
    this_week = _offer(1)
    next_week = replace(
        this_week, source_item_id="2",
        valid_from=date(2026, 10, 15), valid_until=date(2026, 10, 22),
    )
    unique, _, same_offer = dedupe([this_week, next_week])
    assert same_offer == 0
    assert len(unique) == 2


def test_non_numeric_item_ids_do_not_crash_the_representative_choice():
    unique, _, same_offer = dedupe([_offer("abc"), _offer(5)])
    assert same_offer == 1
    assert unique[0].source_item_id == "5"  # numeric sorts ahead of non-numeric


# --- collect_deals over the provider seam -----------------------------------

def test_collect_deals_searches_every_term_and_reports(raw_items):
    provider = FakeProvider({"chicken breast": raw_items, "milk": []})
    deals, report = collect_deals(
        provider, "M5V3A8", terms=["chicken breast", "milk"]
    )

    assert provider.calls == [("chicken breast", "M5V3A8"), ("milk", "M5V3A8")]
    assert report.terms_searched == 2
    assert report.raw_items == len(raw_items)
    assert report.normalized == len(deals) > 0
    assert report.terms_failed == []


def test_one_failing_term_does_not_lose_the_run(raw_items):
    provider = FakeProvider({"chicken breast": raw_items}, fail_terms={"milk"})
    deals, report = collect_deals(
        provider, "M5V3A8", terms=["milk", "chicken breast"]
    )

    assert report.terms_failed == ["milk"]
    assert report.terms_searched == 2
    assert len(deals) > 0  # the good term still landed


def test_report_surfaces_regular_price_coverage(raw_items):
    _, report = collect_deals(FakeProvider({"t": raw_items}), "M5V3A8", terms=["t"])
    assert 0.0 < report.regular_price_coverage < 0.5
    assert report.sale_price_only > 0


def test_report_coverage_is_zero_not_crash_on_empty_run():
    _, report = collect_deals(FakeProvider({}), "M5V3A8", terms=["nope"])
    assert report.normalized == 0
    assert report.regular_price_coverage == 0.0


def test_report_as_dict_is_json_serialisable(raw_items):
    _, report = collect_deals(FakeProvider({"t": raw_items}), "M5V3A8", terms=["t"])
    report.observed_on = datetime(2026, 10, 14, tzinfo=timezone.utc)
    payload = json.dumps(report.as_dict())
    assert "regular_price_coverage" in payload
