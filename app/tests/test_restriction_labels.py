"""Unsupported restriction labels are refused, never silently "approved".

A label is supported exactly when ``products.RESTRICTION_ATTRIBUTES`` maps it
to curated product attributes. Anything else (``halal`` today) is refused by
the user API (422), by recipe generation (422 at the route, and an exception
in ``run_recipe_generation`` for any other caller), and -- defence in depth --
is a violation of its own in the deterministic recipe verdict.

Offline: routers are mounted on a bare FastAPI app, driven through
``httpx.ASGITransport`` (see CLAUDE.md on TestClient), with the DB stubbed.
"""
from datetime import datetime

import pytest
from fastapi import FastAPI

from app import main_recipe_generation
from app.routes import recipes as recipes_route
from app.routes import users as users_route
from app.services import products
from app.services.recipe_jobs import RecipeJobRegistry
from app.tests.test_recipe_jobs import AsgiClient


class _Client(AsgiClient):
    def put(self, path, **kwargs):
        return self._request("PUT", path, **kwargs)


def _stored(**overrides):
    row = {"user_id": 7, "email": "a@example.com", "postal_code": "M5V3A8",
           "budget": 75.0, "budget_period": "monthly", "household_size": 2,
           "dietary_restrictions": ["kosher"], "created_at": datetime(2026, 10, 1),
           "is_active": True}
    row.update(overrides)
    return row


@pytest.fixture
def users_client(monkeypatch):
    calls = []

    def create(data):
        calls.append(("create", data))
        return _stored(dietary_restrictions=data.dietary_restrictions)

    def update(user_id, data):
        calls.append(("update", data))
        return _stored(dietary_restrictions=data.dietary_restrictions or [])

    monkeypatch.setattr(users_route.UserService, "create_user", staticmethod(create))
    monkeypatch.setattr(users_route.UserService, "update_user", staticmethod(update))
    app = FastAPI()
    app.include_router(users_route.router)
    client = _Client(app)
    client.calls = calls
    return client


BODY = {"email": "a@example.com", "postal_code": "M5V 3A8", "budget": 75,
        "household_size": 2}


def test_supported_list_is_exposed_and_is_the_enforced_table(users_client):
    res = users_client.get("/users/dietary-restrictions")
    assert res.status_code == 200
    assert res.json() == {"supported": sorted(products.RESTRICTION_ATTRIBUTES)}
    assert "kosher" in res.json()["supported"]
    assert "halal" not in res.json()["supported"]


@pytest.mark.parametrize("labels", [["halal"], ["vegan", "Halal"], ["keto"]])
def test_register_refuses_an_unsupported_label_with_422(users_client, labels):
    res = users_client.post("/users/register",
                            json={**BODY, "dietary_restrictions": labels})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert isinstance(detail, str)
    assert "Unsupported dietary restriction" in detail
    for label in products.supported_restrictions():
        assert label in detail
    assert users_client.calls == []          # nothing stored


def test_register_accepts_kosher(users_client):
    res = users_client.post("/users/register",
                            json={**BODY, "dietary_restrictions": ["kosher", "vegan"]})
    assert res.status_code == 201, res.text
    assert res.json()["dietary_restrictions"] == ["kosher", "vegan"]


def test_update_refuses_an_unsupported_label_with_422(users_client):
    res = users_client.put("/users/7", json={"dietary_restrictions": ["halal"]})
    assert res.status_code == 422
    assert "halal" in res.json()["detail"]
    assert users_client.calls == []


def test_update_without_restrictions_is_not_checked(users_client):
    res = users_client.put("/users/7", json={"household_size": 3})
    assert res.status_code == 200, res.text


def test_update_of_a_missing_user_is_404_not_500(users_client, monkeypatch):
    monkeypatch.setattr(users_route.UserService, "update_user",
                        staticmethod(lambda user_id, data: None))
    res = users_client.put("/users/999", json={"dietary_restrictions": ["kosher"]})
    assert res.status_code == 404


# --------------------------------------------------------------------------
# Generation for a stored profile carrying an unsupported label
# --------------------------------------------------------------------------

@pytest.fixture
def recipes_client(monkeypatch):
    user = _stored(dietary_restrictions=["vegetarian", "halal"])
    monkeypatch.setattr(recipes_route.UserService, "get_user_by_id",
                        staticmethod(lambda user_id: user))
    registry = RecipeJobRegistry()
    monkeypatch.setattr(recipes_route, "job_registry", registry)
    ran = []
    monkeypatch.setattr(main_recipe_generation, "run_recipe_generation",
                        lambda **kw: ran.append(kw))
    app = FastAPI()
    app.include_router(recipes_route.router)
    client = AsgiClient(app)
    client.ran = ran
    client.registry = registry
    return client


def test_generation_refuses_a_stored_unsupported_label(recipes_client):
    res = recipes_client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "halal" in detail and "kosher" in detail
    assert recipes_client.ran == []          # no job, no graph


def test_run_recipe_generation_refuses_before_any_work(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the graph must not be built")

    monkeypatch.setattr(main_recipe_generation, "create_recipe_generation_graph", boom)
    with pytest.raises(products.UnsupportedRestrictionError) as err:
        main_recipe_generation.run_recipe_generation(
            user_id=1, postal_code="M5V3A8", budget=50.0, household_size=1,
            dietary_restrictions=["halal"], num_meals=1)
    assert err.value.labels == ["halal"]
