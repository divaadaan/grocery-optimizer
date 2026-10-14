"""Tests for the ingestion write path, against a fake cursor.

No database. These are not a substitute for running the SQL — they cannot
catch a Postgres syntax error — but they do catch the mistakes that are easy
to make and invisible until production: a ``%(name)s`` placeholder with no
matching parameter, a store re-inserted once per deal, a sale-only price
written into the shelf-price column, and a snapshot timestamp that is not
truncated to the day.

The migrations these statements depend on (001-003) have not been applied to
any database, so real execution remains unverified. See the accompanying
report.
"""
import re
from datetime import date, datetime, timezone

import pytest

from app.services.flipp_ingestion import (
    _DEAL_UPSERT,
    _SNAPSHOT_INSERT,
    _STORE_INSERT,
    _STORE_SELECT,
    IngestionReport,
    NormalizedDeal,
    _observation_day,
    persist_deals,
)

PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def _deal(**overrides):
    base = dict(
        product_name="CHICKEN BREAST",
        merchant_name="No Frills",
        merchant_id="2332",
        sale_price=4.88,
        regular_price=None,
        unit="lb",
        category="Meat & Poultry",
        valid_from=date(2026, 10, 15),
        valid_until=date(2026, 10, 22),
        source_item_id="1047100125",
        search_term="chicken breast",
    )
    base.update(overrides)
    return NormalizedDeal(**base)


class FakeCursor:
    """Records every statement and checks its parameters are complete."""

    def __init__(self, store_insert_returns=True):
        self.calls = []
        self.store_insert_returns = store_insert_returns
        self._next = None

    def execute(self, sql, params=None):
        params = params or {}
        needed = set(PLACEHOLDER.findall(sql))
        missing = needed - set(params)
        assert not missing, f"unbound placeholder(s) {sorted(missing)} in {sql[:60]!r}"
        unused = set(params) - needed
        assert not unused, f"unused parameter(s) {sorted(unused)} for {sql[:60]!r}"

        self.calls.append((sql, dict(params)))

        if sql is _STORE_INSERT:
            self._next = {"store_id": 42} if self.store_insert_returns else None
        elif sql is _STORE_SELECT:
            self._next = {"store_id": 42}
        elif sql is _DEAL_UPSERT:
            self._next = {"deal_id": 7, "was_inserted": True}
        elif sql is _SNAPSHOT_INSERT:
            self._next = {"snapshot_id": 9}
        else:  # pragma: no cover
            self._next = None

    def fetchone(self):
        return self._next

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # -- helpers for assertions -------------------------------------------
    def params_for(self, sql):
        return [p for s, p in self.calls if s is sql]

    def count(self, sql):
        return len(self.params_for(sql))


class FakeDatabase:
    def __init__(self, cursor):
        self.cursor = cursor

    def get_connection(self):
        db = self

        class Conn:
            def cursor(self_inner):
                return db.cursor

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

        class Ctx:
            def __enter__(self_inner):
                return Conn()

            def __exit__(self_inner, *exc):
                return False

        return Ctx()


def _persist(deals, **kwargs):
    cursor = FakeCursor(**kwargs)
    report = IngestionReport(postal_code="M5V3A8", source="flipp")
    persist_deals(
        deals,
        postal_code="M5V3A8",
        source="flipp",
        report=report,
        observed_on=datetime(2026, 10, 14, tzinfo=timezone.utc),
        database=FakeDatabase(cursor),
    )
    return cursor, report


# --- every statement's parameters are complete ------------------------------

def test_all_placeholders_are_bound():
    # FakeCursor.execute asserts this; this test is the one that runs it.
    cursor, _ = _persist([_deal()])
    assert cursor.count(_STORE_INSERT) == 1
    assert cursor.count(_DEAL_UPSERT) == 1
    assert cursor.count(_SNAPSHOT_INSERT) == 1


@pytest.mark.parametrize("sql", [_STORE_INSERT, _STORE_SELECT, _DEAL_UPSERT,
                                 _SNAPSHOT_INSERT])
def test_statements_are_parameterised_not_interpolated(sql):
    """No f-string or % formatting of values into the SQL."""
    assert PLACEHOLDER.search(sql), sql[:60]
    assert "%s" not in PLACEHOLDER.sub("", sql), "positional placeholder mixed in"


# --- store upsert -----------------------------------------------------------

def test_store_is_looked_up_once_per_merchant_not_once_per_deal():
    deals = [_deal(source_item_id=str(i)) for i in range(5)]
    cursor, report = _persist(deals)
    assert cursor.count(_STORE_INSERT) == 1
    assert cursor.count(_DEAL_UPSERT) == 5
    assert report.stores_created == 1


def test_two_merchants_are_two_stores():
    deals = [
        _deal(merchant_name="No Frills", merchant_id="2332", source_item_id="1"),
        _deal(merchant_name="Metro", merchant_id="2018", source_item_id="2"),
    ]
    cursor, report = _persist(deals)
    assert cursor.count(_STORE_INSERT) == 2
    assert report.stores_created == 2


def test_existing_store_falls_back_to_select_and_is_not_counted_as_created():
    cursor, report = _persist([_deal()], store_insert_returns=False)
    assert cursor.count(_STORE_INSERT) == 1
    assert cursor.count(_STORE_SELECT) == 1
    assert report.stores_created == 0


def test_store_rows_carry_the_queried_postal_code():
    # A flyer item has no address, so the postal code we searched IS the scope.
    cursor, _ = _persist([_deal()])
    params = cursor.params_for(_STORE_INSERT)[0]
    assert params["postal_code"] == "M5V3A8"
    assert params["source"] == "flipp"
    assert params["merchant_id"] == "2332"


def test_store_upsert_raises_if_it_neither_inserts_nor_matches():
    """Rather than silently writing deals against a store_id of None."""
    cursor = FakeCursor(store_insert_returns=False)
    original = cursor.execute

    def execute(sql, params=None):
        original(sql, params)
        if sql is _STORE_SELECT:
            cursor._next = None

    cursor.execute = execute
    with pytest.raises(RuntimeError, match="migrations/001"):
        persist_deals(
            [_deal()], postal_code="M5V3A8", source="flipp",
            report=IngestionReport(postal_code="M5V3A8", source="flipp"),
            database=FakeDatabase(cursor),
        )


# --- price / sale_price split ----------------------------------------------

def test_sale_only_deal_leaves_the_shelf_price_column_null():
    """The core honesty rule: price is a SHELF price. With only a promo price
    from the source, it stays NULL and price_basis records why."""
    cursor, _ = _persist([_deal(regular_price=None)])
    snap = cursor.params_for(_SNAPSHOT_INSERT)[0]
    assert snap["price"] is None
    assert snap["sale_price"] == 4.88
    assert snap["price_basis"] == "sale_only"


def test_regular_and_sale_deal_fills_both_price_columns():
    cursor, _ = _persist([_deal(sale_price=29.99, regular_price=39.99)])
    snap = cursor.params_for(_SNAPSHOT_INSERT)[0]
    assert snap["price"] == 39.99
    assert snap["sale_price"] == 29.99
    assert snap["price_basis"] == "regular_and_sale"


def test_deal_row_keeps_regular_price_null_rather_than_copying_the_sale_price():
    cursor, _ = _persist([_deal(regular_price=None)])
    deal_params = cursor.params_for(_DEAL_UPSERT)[0]
    assert deal_params["regular_price"] is None
    assert deal_params["discount_percentage"] is None


# --- snapshot timing --------------------------------------------------------

def test_snapshot_time_is_the_run_day_not_the_run_instant():
    """Two refreshes on the same day must collapse, not double-weight the day
    in the trailing median."""
    cursor, report = _persist([_deal()])
    snap = cursor.params_for(_SNAPSHOT_INSERT)[0]
    assert snap["observed_on"] == datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert report.observed_on == snap["observed_on"]


def test_observation_day_truncates_to_utc_midnight():
    noon = datetime(2026, 10, 14, 17, 43, 12, 500, tzinfo=timezone.utc)
    assert _observation_day(noon) == datetime(2026, 10, 14, tzinfo=timezone.utc)


def test_observation_day_is_timezone_aware():
    assert _observation_day().tzinfo is not None


# --- idempotency is expressed in SQL, not in Python -------------------------

def test_deal_upsert_conflicts_on_the_natural_key_including_the_flyer_week():
    sql = " ".join(_DEAL_UPSERT.split())
    assert "ON CONFLICT (store_id, source, source_item_id, valid_from) DO UPDATE" in sql


def test_snapshot_insert_is_do_nothing_on_conflict():
    sql = " ".join(_SNAPSHOT_INSERT.split())
    assert "ON CONFLICT (store_id, source, source_item_id, time) DO NOTHING" in sql


def test_deal_upsert_does_not_churn_the_category_on_re_run():
    # First term to find an item owns its category.
    sql = " ".join(_DEAL_UPSERT.split())
    assert "category = COALESCE(deals.category, EXCLUDED.category)" in sql


def test_store_insert_is_do_nothing_so_it_never_triggers_a_stores_update():
    """The origin schema's stores trigger assigns NEW.updated_at, a column
    stores does not have, so any UPDATE on stores raises until migrations/004
    lands. The write path must not depend on that fix."""
    sql = " ".join(_STORE_INSERT.split())
    assert "DO NOTHING" in sql
    assert "DO UPDATE" not in sql


def test_report_counts_inserts_and_snapshots():
    cursor, report = _persist([_deal(source_item_id="1"), _deal(source_item_id="2")])
    assert report.deals_inserted == 2
    assert report.deals_updated == 0
    assert report.snapshots_inserted == 2
    assert report.snapshots_skipped == 0
