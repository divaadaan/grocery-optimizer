"""The Chef keeps selections on the served menu, and judges no product names.

Deals reaching the Chef were already cleared against the user's restrictions
by curated product attributes (``fetch_current_deals``). The Chef's only job
here is making sure the model did not invent something off that menu. No live
LLM: ``invoke_validated`` is replaced with a canned plan.
"""
import pytest

from app.agents import chef_orchestrator as chef_mod
from app.agents.chef_orchestrator import ChefOrchestrator, _restrict_to_served_deals
from app.agents.llm_output import ChefPlan

SERVED = [
    {"deal_id": 1, "product_name": "Peanut Butter", "sale_price": 3.0},
    {"deal_id": 2, "product_name": "Beefsteak Tomatoes", "sale_price": 2.0},
    {"deal_id": 3, "product_name": "Butternut Squash", "sale_price": 1.5},
    {"deal_id": 4, "product_name": "Firm Tofu", "sale_price": 2.5},
]


def _sel(name, deal_id=None):
    return {"product_name": name, "quantity_estimate": "1", "deal_id": deal_id}


def test_served_deals_with_forbidden_looking_names_are_kept():
    """Peanut Butter for a vegan, Beefsteak Tomatoes for a vegetarian: served,
    so kept. The substring filter that used to strip them is gone."""
    groups = [[_sel("Peanut Butter", 1)], [_sel("Beefsteak Tomatoes", 2)],
              [_sel("Butternut Squash", 3)]]
    cleaned, removed = _restrict_to_served_deals(groups, SERVED)
    assert removed == 0
    assert [[s["product_name"] for s in g] for g in cleaned] == [
        ["Peanut Butter"], ["Beefsteak Tomatoes"], ["Butternut Squash"],
    ]


def test_a_hallucinated_product_is_stripped_and_the_group_backfilled():
    groups = [[_sel("Chicken Thighs", 99)], [_sel("Peanut Butter", 1)],
              [_sel("Firm Tofu", 4)]]
    cleaned, removed = _restrict_to_served_deals(groups, SERVED)
    assert removed == 1
    assert all(cleaned), "every group must stay non-empty"
    names = {s["product_name"] for g in cleaned for s in g}
    assert "Chicken Thighs" not in names
    assert names <= {d["product_name"] for d in SERVED}
    # Backfill takes an unused served deal, not one already in a group.
    assert cleaned[0][0]["product_name"] in {"Beefsteak Tomatoes", "Butternut Squash"}


def test_a_selection_matched_by_deal_id_is_rewritten_to_the_served_name():
    """The model got the id right and the name wrong ('tomatoes'): resolve it
    to the deal we served so downstream name lookups find it."""
    groups = [[_sel("tomatoes", "2")], [_sel("Firm Tofu")], [_sel("Peanut Butter")]]
    cleaned, removed = _restrict_to_served_deals(groups, SERVED)
    assert removed == 0
    assert cleaned[0][0]["product_name"] == "Beefsteak Tomatoes"
    assert cleaned[0][0]["deal_id"] == 2
    # Name match fills in a missing id.
    assert cleaned[1][0]["deal_id"] == 4


def test_extra_selection_fields_survive():
    groups = [[{**_sel("Firm Tofu", 4), "note": "press first"}],
              [_sel("Peanut Butter", 1)], [_sel("Butternut Squash", 3)]]
    cleaned, _ = _restrict_to_served_deals(groups, SERVED)
    assert cleaned[0][0]["note"] == "press first"
    assert cleaned[0][0]["quantity_estimate"] == "1"


@pytest.fixture
def chef(monkeypatch):
    monkeypatch.setattr(chef_mod.MLflowLogger, "log_agent_call",
                        staticmethod(lambda *a, **kw: None))
    monkeypatch.setattr(chef_mod.MLflowLogger, "log_ingredient_groups",
                        staticmethod(lambda *a, **kw: None))
    return ChefOrchestrator()


def test_plan_shows_the_model_every_served_deal(monkeypatch, chef):
    """A vegan's served menu, including Peanut Butter, reaches the prompt
    intact and comes back out in the groups."""
    seen = {}

    def fake_invoke(llm, prompt, schema):
        seen["prompt"] = prompt
        plan = ChefPlan(ingredient_groups=[
            [_sel("Peanut Butter", 1)], [_sel("Beefsteak Tomatoes", 2)],
            [_sel("Firm Tofu", 4)],
        ])
        return plan, "{}"

    monkeypatch.setattr(chef_mod, "invoke_validated", fake_invoke)
    state = {
        "available_deals": SERVED,
        "budget": 50.0,
        "household_size": 2,
        "num_meals": 3,
        "dietary_restrictions": ["vegan"],
        "preferences": {},
    }
    update = chef.plan_ingredient_groups(state)

    for deal in SERVED:
        assert deal["product_name"] in seen["prompt"]
    names = [s["product_name"] for g in update["ingredient_groups"] for s in g]
    assert names == ["Peanut Butter", "Beefsteak Tomatoes", "Firm Tofu"]
