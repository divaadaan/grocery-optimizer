"""Shopping lists are priced only against deals the dietary gate lets through.

No database: the route's collaborators are replaced. What matters is that the
user's restrictions reach the deal read, where the products gate applies them
in SQL (see test_deal_gate.py for the gate's semantics).
"""
from types import SimpleNamespace

from app.routes import shopping_lists as route


def test_build_passes_the_users_restrictions_to_the_deal_read(monkeypatch):
    reads = []
    priced_against = []

    def fake_deals(postal_code, category=None, dietary_restrictions=None):
        reads.append((postal_code, dietary_restrictions))
        return [{"deal_id": 1, "product_name": "Peanut Butter"}]

    def fake_optimize(recipes, deals):
        priced_against.append(deals)
        return SimpleNamespace(items=[], total_cost=0.0, estimated_savings=0.0,
                               stores=[])

    monkeypatch.setattr(route.db_service, "get_generation_recipes",
                        lambda user_id, generation_id: [{"recipe_id": 5}])
    monkeypatch.setattr(route.db_service, "save_shopping_list",
                        lambda **kw: 42)
    monkeypatch.setattr(route.StoreService, "get_current_deals_by_postal_code",
                        staticmethod(fake_deals))
    monkeypatch.setattr(route, "optimize_shopping_list", fake_optimize)

    user = {"postal_code": "M5V3A8", "dietary_restrictions": ["vegan"]}
    response = route._build_and_persist(7, user, "gen-1")

    assert reads == [("M5V3A8", ["vegan"])]
    # Whatever the gated read returned is used as-is: no second, name-based
    # filter drops the vegan's peanut butter.
    assert priced_against == [[{"deal_id": 1, "product_name": "Peanut Butter"}]]
    assert response.list_id == 42
