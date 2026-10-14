"""Tests for the admin review routes, with the service layer stubbed.

No database. The service functions these routes call are tested against the
real schema elsewhere; what matters here is the HTTP contract, and especially
the fail-closed rules -- a route that let an alias be confirmed without a
product would hand a restricted user a deal whose attributes nothing defines.

Driven through ``httpx.ASGITransport`` rather than starlette's ``TestClient``,
which is incompatible with the installed httpx (see CLAUDE.md).
"""
import httpx
import pytest

from app.main import app
from app.services import products

ATTRS = list(products.ATTRIBUTES)


def _group(**overrides):
    base = {
        "product_id": 1,
        "canonical_name": "milk",
        "category": "dairy",
        "product_status": "proposed",
        "is_pantry_staple": False,
        "contains_meat": False,
        "contains_fish": False,
        "contains_dairy": True,
        "contains_egg": False,
        "contains_nuts": False,
        "times_seen": 8,
        "clears": ["vegetarian", "pescatarian", "no_nuts"],
        "oracle_disagreement": [],
        "aliases": [
            {"alias_id": 11, "raw_example": "BEATRICE MILK", "times_seen": 4,
             "confidence": 0.95, "proposal_reason": "cow's milk", "source": "x",
             "normalized_name": "beatrice milk"},
            {"alias_id": 12, "raw_example": "NEILSON MILK 4L", "times_seen": 4,
             "confidence": 0.95, "proposal_reason": None, "source": "x",
             "normalized_name": "neilson milk"},
        ],
    }
    base.update(overrides)
    return base


ORPHAN = {
    "alias_id": 99, "raw_example": "MILK OR JUICE OR CHEESE", "times_seen": 1,
    "confidence": 0.9, "proposal_reason": "three products", "source": "x",
    "normalized_name": "milk or juice or cheese", "product_id": None,
    "canonical_name": None, "category": None, "product_status": None,
    "is_pantry_staple": None,
    **{attr: None for attr in ATTRS},
}

STATS = {"proposed": 3, "confirmed": 0, "rejected": 0,
         "products_confirmed": 0, "deals_linked": 0, "deals_total": 10}


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def stub(monkeypatch):
    """Replace every DB-touching service call with a recorder."""
    calls = {"confirm": [], "reject": [], "reject_product": [],
             "set_attributes": [], "link": 0}

    monkeypatch.setattr(products, "pending_proposals",
                        lambda **kw: [ORPHAN])
    monkeypatch.setattr(products, "group_proposals",
                        lambda rows: ([_group()], [ORPHAN]))
    monkeypatch.setattr(products, "review_stats", lambda **kw: dict(STATS))

    def _confirm(product_id, alias_ids, reviewer, **kw):
        calls["confirm"].append((product_id, list(alias_ids), reviewer))
        return len(alias_ids)

    def _reject(alias_ids, reviewer, **kw):
        calls["reject"].append((list(alias_ids), reviewer))
        return len(alias_ids)

    def _reject_product(product_id, reviewer, **kw):
        calls["reject_product"].append((product_id, reviewer))
        return True

    def _set_attributes(product_id, updates, **kw):
        unknown = set(updates) - set(products.ATTRIBUTES)
        if unknown:
            raise ValueError(f"unknown attribute(s): {sorted(unknown)}")
        calls["set_attributes"].append((product_id, dict(updates)))
        return {k: bool(v) for k, v in updates.items()}

    def _link(**kw):
        calls["link"] += 1
        return {"examined": 2, "linked_deals": 5, "linked_snapshots": 5,
                "unmapped": 1}

    monkeypatch.setattr(products, "confirm_group", _confirm)
    monkeypatch.setattr(products, "reject_aliases", _reject)
    monkeypatch.setattr(products, "reject_product_if_orphaned", _reject_product)
    monkeypatch.setattr(products, "set_attributes", _set_attributes)
    monkeypatch.setattr(products, "link_deals", _link)
    return calls


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_review_page_is_served_as_html(client):
    async with client as http:
        res = await http.get("/admin/review")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    # Must not be cached: a stale page shows already-decided items and invites
    # double-reviewing.
    assert res.headers.get("cache-control") == "no-store"
    assert "Product review" in res.text


@pytest.mark.asyncio
async def test_page_is_self_contained(client):
    """No CDN, no build step. It has to work offline against localhost."""
    async with client as http:
        res = await http.get("/admin/review")
    body = res.text
    assert "<script" in body and "<style" in body
    for marker in ("src=\"http", "src='http", "href=\"http", "cdn."):
        assert marker not in body


@pytest.mark.asyncio
async def test_admin_routes_are_not_under_the_versioned_api_prefix(client):
    """It is an internal tool; no client should look like it depends on it."""
    async with client as http:
        res = await http.get("/api/v1/admin/review")
    assert res.status_code == 404


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_returns_groups_orphans_and_stats(client, stub):
    async with client as http:
        res = await http.get("/admin/products/queue")
    assert res.status_code == 200
    body = res.json()
    assert body["groups"][0]["canonical_name"] == "milk"
    assert body["orphans"][0]["alias_id"] == 99
    assert body["stats"] == STATS
    assert body["attributes"] == ATTRS
    assert "vegan" in body["labels"]


@pytest.mark.asyncio
async def test_queue_exposes_the_oracle_alarm_field(client, stub):
    async with client as http:
        res = await http.get("/admin/products/queue")
    assert "oracle_disagreement" in res.json()["groups"][0]


# --------------------------------------------------------------------------
# Decide — fail-closed rules
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_requires_a_product(client, stub):
    """THE fail-closed rule. An alias with no product has no attributes to
    read, so confirming it could only ever mean 'serve something unknown'."""
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": None, "alias_ids": [99], "action": "confirm",
        })
    assert res.status_code == 400
    assert "reject it instead" in res.json()["detail"]
    assert stub["confirm"] == []


@pytest.mark.asyncio
async def test_decide_rejects_an_empty_alias_list(client, stub):
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": 1, "alias_ids": [], "action": "confirm",
        })
    assert res.status_code == 400
    assert stub["confirm"] == []


@pytest.mark.asyncio
async def test_unknown_attribute_is_a_400_not_a_silent_noop(client, stub):
    """Attribute names reach SQL by interpolation, so the whitelist matters.

    A silently-ignored unknown attribute would also mean a reviewer could
    think they had set something they had not.
    """
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": 1, "alias_ids": [11], "action": "confirm",
            "attributes": {"contains_meat": True, "contains_sqlinjection": True},
        })
    assert res.status_code == 400
    assert "contains_sqlinjection" in res.json()["detail"]
    assert stub["confirm"] == []


@pytest.mark.asyncio
async def test_bad_action_is_rejected_by_validation(client, stub):
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": 1, "alias_ids": [11], "action": "promote",
        })
    assert res.status_code == 422


# --------------------------------------------------------------------------
# Decide — happy paths
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_applies_attributes_then_confirms(client, stub):
    attrs = {attr: False for attr in ATTRS} | {"contains_dairy": True}
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": 1, "alias_ids": [11, 12], "action": "confirm",
            "attributes": attrs, "reviewer": "divad",
        })
    assert res.status_code == 200
    assert res.json()["decided"] == 2
    # Attributes are written BEFORE the confirm, so a confirmed row can never
    # carry the pre-edit labels.
    assert stub["set_attributes"] == [(1, attrs)]
    assert stub["confirm"] == [(1, [11, 12], "divad")]


@pytest.mark.asyncio
async def test_reject_rejects_aliases_and_orphans_the_product(client, stub):
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": 1, "alias_ids": [11, 12], "action": "reject",
            "reviewer": "divad",
        })
    assert res.status_code == 200
    assert res.json()["decided"] == 2
    assert res.json()["product_rejected"] is True
    assert stub["reject"] == [([11, 12], "divad")]
    assert stub["reject_product"] == [(1, "divad")]


@pytest.mark.asyncio
async def test_reject_without_a_product_works_for_orphans(client, stub):
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "alias_ids": [99], "action": "reject",
        })
    assert res.status_code == 200
    assert stub["reject"] == [([99], "web")]
    assert stub["reject_product"] == []


@pytest.mark.asyncio
async def test_reviewer_defaults_rather_than_failing(client, stub):
    async with client as http:
        await http.post("/admin/products/decide", json={
            "alias_ids": [99], "action": "reject",
        })
    assert stub["reject"][0][1] == "web"


@pytest.mark.asyncio
async def test_every_decision_returns_fresh_stats(client, stub):
    """The UI header must not drift from the database after a decision."""
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": 1, "alias_ids": [11], "action": "confirm",
        })
    assert res.json()["stats"] == STATS


# --------------------------------------------------------------------------
# Link
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_link_runs_the_deterministic_step(client, stub):
    async with client as http:
        res = await http.post("/admin/products/link", json={})
    assert res.status_code == 200
    assert res.json()["link"]["linked_deals"] == 5
    assert res.json()["stats"] == STATS
    assert stub["link"] == 1
