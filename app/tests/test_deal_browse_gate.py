"""The deal browse routes (deals / top-deals / search) apply the dietary gate
when the caller passes the user's restrictions.

No database: the service's cursor is faked to capture the SQL, and the route
tests patch ``StoreService``. What is under test is that the products-gate
fragment from ``deal_gate.deal_filter`` reaches every browse query, that
omitting restrictions keeps the no-user-context behaviour, and that an
unsupported label is refused rather than served unfiltered.
"""
from contextlib import contextmanager

import pytest
from fastapi import FastAPI

from app.routes import stores as stores_route
from app.services import store_service as store_service_mod
from app.services.deal_gate import deal_filter
from app.services.store_service import StoreService
from app.tests.test_stores_refresh_route import AsgiClient


class _Cursor:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, query, params):
        self.sink.append((query, params))

    def fetchall(self):
        return []


@pytest.fixture
def captured(monkeypatch):
    sink = []

    @contextmanager
    def fake_cursor(*_a, **_k):
        yield _Cursor(sink)

    monkeypatch.setattr(store_service_mod.db, "get_cursor", fake_cursor)
    return sink


@pytest.mark.parametrize("call", [
    lambda r: StoreService.get_top_deals("A1A1A1", 20, r),
    lambda r: StoreService.search_deals("A1A1A1", "beans", r),
])
def test_top_and_search_queries_carry_the_products_gate(captured, call):
    call(["vegetarian"])
    query, params = captured[-1]
    fragment, extra = deal_filter(["vegetarian"])
    assert fragment and fragment.strip() in query
    for key, value in extra.items():
        assert params[key] == value
    # The gate sits before ORDER BY / LIMIT, so it filters before the cap.
    assert query.index(fragment.strip()) < query.index("ORDER BY")


@pytest.mark.parametrize("call", [
    lambda r: StoreService.get_top_deals("A1A1A1", 20, r),
    lambda r: StoreService.search_deals("A1A1A1", "beans", r),
])
def test_no_restrictions_means_no_gate(captured, call):
    call(None)
    query, _ = captured[-1]
    assert "deal_products" not in query


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(stores_route.router)
    return AsgiClient(app)


def _deal(i):
    return {"deal_id": i, "product_name": f"p{i}", "brand": None, "sale_price": 1.0,
            "regular_price": 2.0, "discount_percentage": 50, "unit": "ea",
            "category": "Produce", "valid_from": "2026-11-20", "valid_until": "2026-11-27",
            "store_name": "S", "chain": "S"}


@pytest.mark.parametrize("path,method", [
    ("/deals/A1A1A1", "get_current_deals_by_postal_code"),
    ("/top-deals/A1A1A1", "get_top_deals"),
    ("/search/A1A1A1?q=beans", "search_deals"),
])
def test_routes_pass_restrictions_through(client, monkeypatch, path, method):
    seen = {}

    def fake(*args):
        seen["args"] = args
        return [_deal(1)]

    monkeypatch.setattr(StoreService, method, staticmethod(fake))
    sep = "&" if "?" in path else "?"
    res = client.get(f"{stores_route.router.prefix}{path}{sep}restrictions=vegetarian&restrictions=nut_free")
    assert res.status_code == 200, res.text
    assert seen["args"][-1] == ["vegetarian", "nut_free"]


@pytest.mark.parametrize("path", ["/deals/A1A1A1", "/top-deals/A1A1A1", "/search/A1A1A1?q=beans"])
def test_unsupported_restriction_is_refused(client, monkeypatch, path):
    for m in ("get_current_deals_by_postal_code", "get_top_deals", "search_deals"):
        monkeypatch.setattr(StoreService, m, staticmethod(lambda *a: pytest.fail("served")))
    sep = "&" if "?" in path else "?"
    res = client.get(f"{stores_route.router.prefix}{path}{sep}restrictions=halal")
    assert res.status_code == 422
    assert "halal" in res.text
