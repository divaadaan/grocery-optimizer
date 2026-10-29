"""Unknown savings reaches the API as null, never as a fabricated 0.

Driven through ``httpx.ASGITransport`` (starlette's TestClient is broken with
the installed httpx; see CLAUDE.md). No database, no LLM.
"""
import httpx
import pytest

from app.agents import graph
from app.main import app
from app.routes import recipes as recipes_route
from app.routes import shopping_lists as route

DEAL_NO_REGULAR = {"deal_id": 1, "product_name": "Pasta Penne", "sale_price": 1.49,
                   "regular_price": None, "store_name": "Store A", "category": "Pasta"}


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


@pytest.mark.asyncio
async def test_shopping_list_without_regular_prices_has_null_savings(client, monkeypatch):
    saved = {}

    def save(**kw):
        saved.update(kw)
        return 42

    monkeypatch.setattr(route.UserService, "get_user_by_id",
                        staticmethod(lambda uid: {"postal_code": "M5V3A8"}))
    monkeypatch.setattr(route.db_service, "get_latest_generation_id", lambda uid: "gen-1")
    monkeypatch.setattr(route.db_service, "get_generation_recipes", lambda uid, gid: [{
        "recipe_id": 5,
        "ingredients": [{"name": "penne", "deal_id": 1},
                        {"name": "salt", "product_id": 9},
                        {"name": "old uncited", "price": 2.0}],
    }])
    monkeypatch.setattr(route.db_service, "save_shopping_list", save)
    monkeypatch.setattr(route.StoreService, "get_current_deals_by_postal_code",
                        staticmethod(lambda *a, **k: [DEAL_NO_REGULAR]))

    async with client as http:
        response = await http.post("/api/v1/shopping-list/7/generate")

    assert response.status_code == 201
    body = response.json()
    assert body["estimated_savings"] is None
    assert float(body["total_cost"]) == 1.49
    assert body["stores"] == ["Store A"]
    products = {i["product"]: i for i in body["items"]}
    assert set(products) == {"Pasta Penne", "old uncited"}  # staple absent
    assert products["old uncited"]["price"] is None
    # Persisted as NULL too: both columns are nullable.
    assert saved["estimated_savings"] is None
    assert saved["regular_total"] is None


@pytest.mark.asyncio
async def test_stored_list_with_null_savings_round_trips(client, monkeypatch):
    monkeypatch.setattr(route.UserService, "get_user_by_id",
                        staticmethod(lambda uid: {"postal_code": "M5V3A8"}))
    monkeypatch.setattr(route.db_service, "get_latest_generation_id", lambda uid: "gen-1")
    monkeypatch.setattr(route.db_service, "get_latest_shopping_list",
                        lambda uid, generation_id=None: {
        "list_id": 1, "user_id": 7, "recipe_ids": [5], "items": [],
        "total_cost": 0, "estimated_savings": None, "created_at": "2026-01-01T00:00:00",
        "is_completed": False,
    })
    async with client as http:
        response = await http.get("/api/v1/shopping-list/7")
    assert response.status_code == 200
    assert response.json()["estimated_savings"] is None


def test_recipe_generation_response_carries_null_savings():
    result = {
        "approved_recipe_ids": [], "generated_recipes": {}, "validation_results": {},
        "total_cost": 0.0, "cost_per_meal": 0.0, "estimated_savings": None,
        "status": "completed",
    }
    response = recipes_route._build_generation_response(result, 0.1)
    assert response.model_dump()["estimated_savings"] is None


def test_graph_savings_is_none_when_no_cited_deal_has_a_regular_price():
    state = {"available_deals": [DEAL_NO_REGULAR]}
    recipes = [{"ingredients": [{"name": "penne", "deal_id": 1}]}]
    assert graph._savings(state, recipes) is None


def test_graph_savings_is_a_number_when_a_regular_price_exists():
    deal = dict(DEAL_NO_REGULAR, regular_price=2.99)
    recipes = [{"ingredients": [{"name": "penne", "deal_id": 1}]}]
    assert graph._savings({"available_deals": [deal]}, recipes) == 1.5
