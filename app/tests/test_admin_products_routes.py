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
        "contains_animal_byproduct": False,
        "contains_gluten": False,
        "times_seen": 8,
        "clears": ["vegetarian", "pescatarian", "no_nuts"],
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

UNDECIDED = {
    "product_id": 5, "canonical_name": "strawberry jam", "category": "pantry",
    "status": "confirmed", "is_pantry_staple": True,
    **{attr: False for attr in ATTRS}, "contains_animal_byproduct": None,
    "undecided": ["contains_animal_byproduct"],
    "clears": ["vegetarian", "pescatarian", "no_nuts"], "would_clear": ["vegan"],
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
             "set_attributes": [], "fill": [], "link": 0}

    monkeypatch.setattr(products, "pending_proposals",
                        lambda **kw: [ORPHAN])
    monkeypatch.setattr(products, "undecided_products",
                        lambda **kw: [dict(UNDECIDED)])

    def _fill(product_id, updates, **kw):
        unknown = set(updates) - set(products.ATTRIBUTES)
        if unknown:
            raise ValueError(f"unknown attribute(s): {sorted(unknown)}")
        calls["fill"].append((product_id, dict(updates)))
        if product_id != UNDECIDED["product_id"]:
            return {}
        return {attr: UNDECIDED[attr] for attr in ATTRS} | updates

    monkeypatch.setattr(products, "fill_undecided_attributes", _fill)
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
        # Product 2 stands in for a confirmed product that stores nuts=True.
        if product_id == 2 and updates.get("contains_nuts") is False:
            raise products.AttributeConflict(
                2, "almond milk", {"contains_nuts": (True, False)})
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
async def test_queue_does_not_judge_product_names(client, stub):
    """Review shows the attributes and what they clear, nothing name-derived.

    The substring alarm that used to sit here is gone: curated attributes are
    the only authority, and a name-matching hint mostly cried wolf
    ("beefsteak tomato", "peanut butter").
    """
    async with client as http:
        res = await http.get("/admin/products/queue")
    group = res.json()["groups"][0]
    assert group["clears"] == ["vegetarian", "pescatarian", "no_nuts"]


@pytest.mark.asyncio
async def test_audit_route_is_gone(client):
    async with client as http:
        res = await http.get("/admin/products/audit")
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_review_page_has_no_name_matching_alarm(client):
    async with client as http:
        res = await http.get("/admin/review")
    assert "oracle" not in res.text.lower()


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
async def test_confirming_onto_a_confirmed_product_with_a_flipped_attribute_is_409(
        client, stub):
    """Curated attributes are the dietary authority: a stale tab must not flip
    them, and the alias must stay proposed (no partial write)."""
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": 2, "alias_ids": [21], "action": "confirm",
            "attributes": {"contains_nuts": False, "contains_dairy": False},
        })
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "almond milk" in detail and "contains_nuts" in detail
    assert "stored True, requested False" in detail
    assert "correction step" in detail
    assert stub["confirm"] == []
    assert stub["set_attributes"] == []


@pytest.mark.asyncio
async def test_confirming_onto_a_confirmed_product_with_stored_values_works(
        client, stub):
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": 2, "alias_ids": [21], "action": "confirm",
            "attributes": {"contains_nuts": True},
        })
    assert res.status_code == 200
    assert stub["confirm"] == [(2, [21], "web")]


@pytest.mark.asyncio
async def test_review_page_locks_confirmed_products_and_shows_errors_on_card(client):
    async with client as http:
        page = (await http.get("/admin/review")).text
    assert 'group.product_status === "confirmed"' in page
    assert "group.error" in page


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
# Undecided attributes on confirmed products (migration 015)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_lists_confirmed_products_with_an_undecided_attribute(client, stub):
    async with client as http:
        body = (await http.get("/admin/products/queue")).json()
    assert body["undecided"][0]["product_id"] == 5
    assert body["undecided"][0]["undecided"] == ["contains_animal_byproduct"]
    assert "contains_animal_byproduct" in body["attributes"]
    assert "gelatin" in body["rules"]["contains_meat"]
    assert "honey" in body["rules"]["contains_animal_byproduct"]


@pytest.mark.asyncio
async def test_fill_sets_an_undecided_attribute_and_reports_what_it_clears(client, stub):
    async with client as http:
        res = await http.post("/admin/products/fill", json={
            "product_id": 5, "attributes": {"contains_animal_byproduct": False},
        })
    assert res.status_code == 200
    assert stub["fill"] == [(5, {"contains_animal_byproduct": False})]
    assert "vegan" in res.json()["clears"]


@pytest.mark.asyncio
async def test_fill_rejects_unknown_and_empty_attributes(client, stub):
    async with client as http:
        bad = await http.post("/admin/products/fill", json={
            "product_id": 5, "attributes": {"contains_sqlinjection": True}})
        empty = await http.post("/admin/products/fill", json={
            "product_id": 5, "attributes": {}})
        null = await http.post("/admin/products/fill", json={
            "product_id": 5, "attributes": {"contains_animal_byproduct": None}})
    assert bad.status_code == 400
    assert empty.status_code == 400
    assert null.status_code == 422
    assert stub["fill"] == []


@pytest.mark.asyncio
async def test_fill_on_a_missing_product_is_a_404(client, stub):
    async with client as http:
        res = await http.post("/admin/products/fill", json={
            "product_id": 999, "attributes": {"contains_animal_byproduct": True}})
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_review_page_treats_undecided_as_failing_and_states_the_rule(client):
    """The page's preview must not read null as false, and must not send an
    undecided attribute as false when confirming."""
    async with client as http:
        page = (await http.get("/admin/review")).text
    assert '"contains_animal_byproduct"' in page
    assert "every((a) => group[a] === false)" in page
    assert "attributes[attr] = !!group[attr]" not in page
    assert "Gelatin, lard, tallow and animal" in page


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


@pytest.mark.asyncio
async def test_review_page_knows_the_gluten_attribute_and_rule(client):
    async with client as http:
        page = (await http.get("/admin/review")).text
    assert "contains_gluten" in page
    assert 'gluten_free: ["contains_gluten"]' in page
    assert "certified" in page
