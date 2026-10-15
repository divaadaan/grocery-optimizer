"""Tests for `POST /shopping-list/{user_id}/mark-complete`, the spend event.

No database: ``DatabaseService.mark_shopping_list_complete`` is replaced by a
recorder, because what matters here is the HTTP contract — which list gets
charged to whose budget. Completing a list is what makes its ``total_cost``
count against the user's period budget, so "the latest list" being charged
when the client named a different one is a money bug, not a cosmetic one.

Driven through ``httpx.ASGITransport`` rather than starlette's ``TestClient``,
which is incompatible with the installed httpx (see CLAUDE.md).
"""
import httpx
import pytest

from app.main import app
from app.routes import shopping_lists as route


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def marks(monkeypatch):
    """Record (user_id, list_id) and answer from a small owned-lists table."""
    calls = []
    owned = {7: [11, 12]}  # user 7 owns lists 11 and 12; list 99 is someone else's

    def mark(user_id, list_id=None):
        calls.append((user_id, list_id))
        if list_id is None:
            return bool(owned.get(user_id))
        return list_id in owned.get(user_id, [])

    monkeypatch.setattr(route.db_service, "mark_shopping_list_complete", mark)
    return calls


@pytest.mark.asyncio
async def test_a_named_list_is_the_one_marked(client, marks):
    async with client as http:
        response = await http.post("/api/v1/shopping-list/7/mark-complete?list_id=12")

    assert response.status_code == 204
    assert marks == [(7, 12)]


@pytest.mark.asyncio
async def test_another_users_list_cannot_be_completed(client, marks):
    """Charging list 99 to user 7 would move money on two budgets at once."""
    async with client as http:
        response = await http.post("/api/v1/shopping-list/7/mark-complete?list_id=99")

    assert response.status_code == 404
    assert "99" in response.json()["detail"]
    # The ownership decision is the service's (user_id is in its WHERE
    # clause), so the route must pass both and must not fall back to "latest"
    # when the named list is refused.
    assert marks == [(7, 99)]


@pytest.mark.asyncio
async def test_omitting_list_id_keeps_the_legacy_latest_list_behaviour(client, marks):
    async with client as http:
        response = await http.post("/api/v1/shopping-list/7/mark-complete")

    assert response.status_code == 204
    assert marks == [(7, None)]


@pytest.mark.asyncio
async def test_a_user_with_no_lists_still_404s(client, marks):
    async with client as http:
        response = await http.post("/api/v1/shopping-list/4/mark-complete")

    assert response.status_code == 404
    assert marks == [(4, None)]


@pytest.mark.asyncio
async def test_a_non_numeric_list_id_is_rejected_before_the_database(client, marks):
    async with client as http:
        response = await http.post("/api/v1/shopping-list/7/mark-complete?list_id=latest")

    assert response.status_code == 422
    assert marks == []
