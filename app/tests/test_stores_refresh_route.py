"""Tests for the manual refresh trigger on app/routes/stores.py.

No network and no database: ``run_ingestion`` and ``StoreService`` are both
patched. What is under test is the gating — that nothing reaches the provider
while ingestion is disabled, that a second concurrent refresh is refused, and
that a cold-start auto-ingest cannot be driven once per request.
"""
import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.routes import stores as stores_route
from app.services.flipp_ingestion import IngestionReport


class AsgiClient:
    """Minimal sync HTTP client over an ASGI app.

    `starlette.testclient.TestClient` cannot be used here: starlette 0.35.1
    constructs `httpx.Client(app=...)` and the installed httpx 0.28 removed
    that shortcut. Same approach as app/tests/test_recipe_jobs.py.
    """

    def __init__(self, app):
        self._app = app

    def _request(self, method, path, **kwargs):
        async def go():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._app),
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(go())

    def get(self, path, **kwargs):
        return self._request("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self._request("POST", path, **kwargs)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(stores_route.router)
    return AsgiClient(app)


@pytest.fixture(autouse=True)
def reset_route_state():
    stores_route._last_auto_ingest.clear()
    yield
    stores_route._last_auto_ingest.clear()


@pytest.fixture
def ingestion_enabled(monkeypatch):
    monkeypatch.setattr(settings, "deal_ingestion_enabled", True)


@pytest.fixture
def fake_ingestion(monkeypatch):
    """Replaces run_ingestion; records the calls it would have made."""
    calls = []

    def _run(postal_code, terms=None, **kwargs):
        calls.append({"postal_code": postal_code, "terms": terms})
        report = IngestionReport(postal_code=postal_code, source="flipp")
        report.terms_searched = len(terms) if terms else 50
        report.normalized = 10
        report.sale_price_only = 9
        report.deals_inserted = 8
        report.deals_updated = 2
        report.snapshots_inserted = 10
        report.stores_created = 3
        return report

    monkeypatch.setattr(stores_route, "run_ingestion", _run)
    return calls


@pytest.fixture
def fake_store_reads(monkeypatch):
    """Pretend the DB has one store and (configurably) some deals."""
    state = {"deals": [{"deal_id": 1, "source": "seed"}]}

    monkeypatch.setattr(
        stores_route.StoreService, "get_stores_by_postal_code",
        staticmethod(lambda pc: [{
            "store_id": 1, "name": "No Frills", "chain": "No Frills",
            "postal_code": pc, "address": None, "city": None, "province": None,
        }]),
    )
    monkeypatch.setattr(
        stores_route.StoreService, "get_current_deals_by_postal_code",
        staticmethod(lambda pc, category=None: state["deals"]),
    )
    monkeypatch.setattr(
        stores_route.StoreService, "invalidate_deal_cache",
        staticmethod(lambda pc: 0),
    )
    return state


# --- POST /postal-code/refresh ---------------------------------------------

def test_refresh_is_503_while_ingestion_is_disabled(client, fake_ingestion):
    assert settings.deal_ingestion_enabled is False  # the shipped default
    response = client.post("/postal-code/refresh", json={"postal_code": "M5V3A8"})
    assert response.status_code == 503
    assert "DEAL_INGESTION_ENABLED" in response.json()["detail"]
    assert fake_ingestion == [], "must not touch the provider while disabled"


def test_refresh_runs_ingestion_when_enabled(client, ingestion_enabled, fake_ingestion):
    response = client.post("/postal-code/refresh", json={"postal_code": "M5V 3A8"})
    assert response.status_code == 200
    body = response.json()
    # The request schema normalises the postal code.
    assert body["postal_code"] == "M5V3A8"
    assert fake_ingestion == [{"postal_code": "M5V3A8", "terms": None}]
    assert body["deals_inserted"] == 8
    assert body["snapshots_inserted"] == 10
    assert body["source"] == "flipp"


def test_refresh_surfaces_poor_regular_price_coverage(
    client, ingestion_enabled, fake_ingestion
):
    """The honest warning: savings is meaningless on a batch like this."""
    response = client.post("/postal-code/refresh", json={"postal_code": "M5V3A8"})
    body = response.json()
    assert body["regular_price_coverage"] == 0.1
    assert "estimated_savings is unreliable" in body["message"]


def test_refresh_accepts_ad_hoc_terms(client, ingestion_enabled, fake_ingestion):
    response = client.post(
        "/postal-code/refresh?terms=rice&terms=lentils", json={"postal_code": "M5V3A8"}
    )
    assert response.status_code == 200
    assert fake_ingestion[0]["terms"] == ["rice", "lentils"]


def test_refresh_reports_failed_terms(client, ingestion_enabled, monkeypatch):
    def _run(postal_code, terms=None, **kwargs):
        report = IngestionReport(postal_code=postal_code, source="flipp")
        report.terms_searched = 2
        report.terms_failed = ["milk"]
        return report

    monkeypatch.setattr(stores_route, "run_ingestion", _run)
    body = client.post("/postal-code/refresh", json={"postal_code": "M5V3A8"}).json()
    assert body["terms_failed"] == ["milk"]
    assert "1 term(s) failed" in body["message"]


def test_concurrent_refresh_is_refused_not_queued(client, ingestion_enabled,
                                                  fake_ingestion):
    stores_route._refresh_lock.acquire()
    try:
        response = client.post("/postal-code/refresh", json={"postal_code": "M5V3A8"})
        assert response.status_code == 409
        assert "already running" in response.json()["detail"]
        assert fake_ingestion == [], "must not reach the provider twice over"
    finally:
        stores_route._refresh_lock.release()


def test_refresh_lock_is_released_after_a_run(client, ingestion_enabled,
                                              fake_ingestion):
    client.post("/postal-code/refresh", json={"postal_code": "M5V3A8"})
    assert not stores_route._refresh_lock.locked()
    client.post("/postal-code/refresh", json={"postal_code": "M5V3A8"})
    assert len(fake_ingestion) == 2


def test_refresh_lock_is_released_even_when_ingestion_raises(
    client, ingestion_enabled, monkeypatch
):
    def _boom(postal_code, terms=None, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(stores_route, "run_ingestion", _boom)
    response = client.post("/postal-code/refresh", json={"postal_code": "M5V3A8"})
    assert response.status_code == 500
    assert not stores_route._refresh_lock.locked(), "a failed run must not wedge the lock"


def test_lock_is_loop_agnostic():
    """Regression guard. An asyncio.Lock binds to the first event loop that
    awaits it and raises if reused from another; this code path is reached from
    a fresh loop per request under test and from uvicorn's loop in production.
    """
    assert isinstance(stores_route._refresh_lock, type(threading.Lock()))


# --- POST /postal-code/discover -------------------------------------------

def test_discover_does_not_ingest_when_deals_already_exist(
    client, ingestion_enabled, fake_ingestion, fake_store_reads
):
    """Discover must not become a provider crawl per page load."""
    response = client.post("/postal-code/discover", json={"postal_code": "M5V3A8"})
    assert response.status_code == 200
    assert fake_ingestion == []


def test_discover_ingests_on_a_genuine_cold_start(
    client, ingestion_enabled, fake_ingestion, fake_store_reads
):
    fake_store_reads["deals"] = []
    response = client.post("/postal-code/discover", json={"postal_code": "M5V3A8"})
    assert response.status_code == 200
    assert len(fake_ingestion) == 1
    assert "Refreshed from flipp" in response.json()["message"]


def test_cold_start_ingest_happens_at_most_once_per_cooldown(
    client, ingestion_enabled, fake_ingestion, fake_store_reads
):
    """A postal code with no flyer coverage returns zero deals forever; without
    the cooldown every discover call would re-crawl the provider."""
    fake_store_reads["deals"] = []
    for _ in range(4):
        client.post("/postal-code/discover", json={"postal_code": "M5V3A8"})
    assert len(fake_ingestion) == 1


def test_explicit_refresh_query_param_bypasses_the_cooldown(
    client, ingestion_enabled, fake_ingestion, fake_store_reads
):
    for _ in range(3):
        client.post("/postal-code/discover?refresh=true", json={"postal_code": "M5V3A8"})
    assert len(fake_ingestion) == 3


def test_discover_never_ingests_while_disabled(
    client, fake_ingestion, fake_store_reads
):
    fake_store_reads["deals"] = []
    response = client.post(
        "/postal-code/discover?refresh=true", json={"postal_code": "M5V3A8"}
    )
    assert fake_ingestion == []
    assert "DEAL_INGESTION_ENABLED is off" in response.json()["message"]


# --- GET /postal-code/staples ---------------------------------------------

def test_staples_endpoint_lists_the_terms(client):
    body = client.get("/postal-code/staples").json()
    assert body["count"] > 0
    assert "Meat & Poultry" in body["terms_by_category"]
    assert "chicken breast" in body["terms_by_category"]["Meat & Poultry"]
    assert "flipp_staples.py" in body["note"]
    assert body["count"] == sum(len(v) for v in body["terms_by_category"].values())
