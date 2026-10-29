"""A shopping list is built from ONE generation (Track C3).

It used to be built from the user's last 10 recipes, mixing meal plans. The
route now resolves a single generation -- the explicit ``generation_id`` or
the user's latest -- and lists only its recipes.

Driven through ``httpx.ASGITransport`` (starlette's TestClient is broken with
the installed httpx; see CLAUDE.md). An in-memory stand-in replaces the
route's ``db_service``: no database, no LLM.
"""
import itertools

import httpx
import pytest

from app.main import app
from app.routes import shopping_lists as route

GEN_OLD = "11111111-1111-4111-8111-111111111111"
GEN_NEW = "22222222-2222-4222-8222-222222222222"
GEN_OTHER_USER = "33333333-3333-4333-8333-333333333333"

DEALS = [
    {"deal_id": 1, "product_name": "Penne", "sale_price": 1.50, "regular_price": None,
     "store_name": "Store A", "category": "Pasta"},
    {"deal_id": 2, "product_name": "Tofu", "sale_price": 3.00, "regular_price": None,
     "store_name": "Store B", "category": "Protein"},
    {"deal_id": 3, "product_name": "Rice", "sale_price": 2.00, "regular_price": None,
     "store_name": "Store A", "category": "Grains"},
]


class FakeStore:
    """The route's ``db_service`` surface over in-memory generations."""

    def __init__(self):
        # (recipe_id, user_id, generation_id, cited deal) in save order; GEN_NEW
        # is the user's latest generation.
        self.recipes = [
            {"recipe_id": 10, "user_id": 7, "generation_id": GEN_OLD,
             "ingredients": [{"name": "penne", "deal_id": 1}]},
            {"recipe_id": 11, "user_id": 7, "generation_id": GEN_OLD,
             "ingredients": [{"name": "rice", "deal_id": 3}]},
            {"recipe_id": 20, "user_id": 7, "generation_id": GEN_NEW,
             "ingredients": [{"name": "tofu", "deal_id": 2},
                             {"name": "salt", "product_id": 9}]},
            {"recipe_id": 30, "user_id": 8, "generation_id": GEN_OTHER_USER,
             "ingredients": [{"name": "rice", "deal_id": 3}]},
        ]
        self.generations = {7: [GEN_OLD, GEN_NEW], 8: [GEN_OTHER_USER]}
        self.lists = []
        self._ids = itertools.count(500)

    def get_latest_generation_id(self, user_id):
        gens = self.generations.get(user_id) or []
        return gens[-1] if gens else None

    def get_generation_recipes(self, user_id, generation_id):
        return [r for r in self.recipes
                if r["user_id"] == user_id and r["generation_id"] == generation_id]

    def get_user_recipes(self, user_id, limit=10, generation_id=None):
        raise AssertionError("the shopping list must not read 'the last N recipes'")

    def save_shopping_list(self, **kw):
        row = {**kw, "list_id": next(self._ids), "created_at": "2026-10-29T12:00:00",
               "is_completed": False, "completed_at": None}
        self.lists.append(row)
        return row["list_id"]

    def get_latest_shopping_list(self, user_id, generation_id=None):
        rows = [r for r in self.lists if r["user_id"] == user_id
                and (generation_id is None or r["generation_id"] == generation_id)]
        return rows[-1] if rows else None


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(route, "db_service", fake)
    monkeypatch.setattr(route.UserService, "get_user_by_id",
                        staticmethod(lambda uid: {"user_id": uid, "postal_code": "M5V3A8",
                                                  "dietary_restrictions": []}))
    monkeypatch.setattr(route.StoreService, "get_current_deals_by_postal_code",
                        staticmethod(lambda *a, **k: list(DEALS)))
    return fake


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


def _products(body):
    return sorted(i["product"] for i in body["items"])


@pytest.mark.asyncio
async def test_generate_defaults_to_the_latest_generation_only(client, store):
    async with client as http:
        response = await http.post("/api/v1/shopping-list/7/generate")
    assert response.status_code == 201
    body = response.json()
    assert body["generation_id"] == GEN_NEW
    assert body["recipe_ids"] == [20]
    assert _products(body) == ["Tofu"]          # nothing from GEN_OLD
    assert float(body["total_cost"]) == 3.00
    assert store.lists[-1]["generation_id"] == GEN_NEW


@pytest.mark.asyncio
async def test_generate_for_an_explicit_generation(client, store):
    async with client as http:
        response = await http.post(f"/api/v1/shopping-list/7/generate?generation_id={GEN_OLD}")
    assert response.status_code == 201
    body = response.json()
    assert body["generation_id"] == GEN_OLD
    assert body["recipe_ids"] == [10, 11]
    assert _products(body) == ["Penne", "Rice"]  # nothing from GEN_NEW
    assert float(body["total_cost"]) == 3.50


@pytest.mark.asyncio
async def test_another_users_generation_is_404(client, store):
    async with client as http:
        response = await http.post(
            f"/api/v1/shopping-list/7/generate?generation_id={GEN_OTHER_USER}")
    assert response.status_code == 404
    assert store.lists == []


@pytest.mark.asyncio
async def test_malformed_generation_id_is_422(client, store):
    async with client as http:
        response = await http.get("/api/v1/shopping-list/7?generation_id=not-a-uuid")
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_builds_lazily_per_generation_then_reuses(client, store):
    async with client as http:
        # A stored list for the OLD generation must not be served once a newer
        # generation exists.
        old = await http.post(f"/api/v1/shopping-list/7/generate?generation_id={GEN_OLD}")
        first = await http.get("/api/v1/shopping-list/7")
        again = await http.get("/api/v1/shopping-list/7")
        explicit_old = await http.get(f"/api/v1/shopping-list/7?generation_id={GEN_OLD}")

    assert first.status_code == again.status_code == explicit_old.status_code == 200
    assert first.json()["generation_id"] == GEN_NEW
    assert _products(first.json()) == ["Tofu"]
    # Repeat GET reuses the stored list instead of writing another row.
    assert again.json()["list_id"] == first.json()["list_id"]
    assert len(store.lists) == 2
    assert explicit_old.json()["list_id"] == old.json()["list_id"]


@pytest.mark.asyncio
async def test_user_without_generations_gets_legacy_list_or_404(client, store):
    store.generations[7] = []
    async with client as http:
        missing = await http.get("/api/v1/shopping-list/7")
        store.lists.append({"list_id": 1, "user_id": 7, "recipe_ids": [1, 2],
                            "items": [], "total_cost": 0, "estimated_savings": None,
                            "created_at": "2026-01-01T00:00:00", "is_completed": False,
                            "generation_id": None})
        legacy = await http.get("/api/v1/shopping-list/7")
        generate = await http.post("/api/v1/shopping-list/7/generate")
    assert missing.status_code == 404
    assert legacy.status_code == 200
    assert legacy.json()["generation_id"] is None
    assert generate.status_code == 404
