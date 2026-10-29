"""Track C1: the recipe-orchestration core, offline and deterministic.

Cost per unique deal, the post-generation budget check, recipe-count
enforcement (num_meals 1, 2, 3, 7), deal_id-only resolution, preferences in
the SousChef prompts, the retry strategies, and the combination-rule (kosher)
prompt hints. The real compiled graph runs with scripted chat models and
in-memory deps (the ``test_graph_offline`` fakes); nothing reaches Postgres,
MLflow, a network or a live model.
"""
import json
import re
import threading

import pytest

from app import main_recipe_generation
from app.agents import graph as graph_mod
from app.agents.chef_orchestrator import ChefOrchestrator, _merge_groups
from app.agents.costing import deals_by_id, price_plan, price_recipe
from app.agents.deps import GraphDeps, InMemoryRecipeStore
from app.agents.prompts import combination_rule_hints
from app.agents.sous_chef import SousChef
from app.routes import recipes as route
from app.services import products
from app.tests.test_graph_offline import (  # noqa: F401  (autouse fixture)
    FakeCatalog,
    FakeDeals,
    RecordingTracker,
    ScriptedChatModel,
    no_database_no_mlflow,
)

ASSIGNED = "**Your Assigned Deals (cite each by its deal_id):**"
RETRY_MARKER = "revising a rejected recipe"
_TARGET = re.compile(r"Number of recipes to create: (\d+)")

# Two stores sell "BROCCOLI CROWNS": two deals, one name.
DEALS = [
    {"deal_id": 1, "product_name": "FIRM TOFU", "sale_price": 3.00, "regular_price": None,
     "store_name": "FreshCo"},
    {"deal_id": 2, "product_name": "BROCCOLI CROWNS", "sale_price": 2.00,
     "regular_price": None, "store_name": "FreshCo"},
    {"deal_id": 3, "product_name": "BASMATI RICE", "sale_price": 5.00, "regular_price": None,
     "store_name": "FreshCo"},
    {"deal_id": 4, "product_name": "BROCCOLI CROWNS", "sale_price": 1.50,
     "regular_price": None, "store_name": "No Frills"},
    {"deal_id": 5, "product_name": "DICED TOMATOES", "sale_price": 1.00,
     "regular_price": None, "store_name": "No Frills"},
    {"deal_id": 6, "product_name": "CHICKPEAS", "sale_price": 1.25, "regular_price": None,
     "store_name": "No Frills"},
]
STAPLES = [{"product_id": 101, "canonical_name": "Salt", "status": "confirmed",
            **{a: False for a in products.ATTRIBUTES}}]
NUTRITIONIST = json.dumps({"approved": True, "feedback": "ok",
                           "nutrition_facts": {"calories_per_serving": 400},
                           "health_score": 70})


def _sel(deal_id, name=None):
    deal = next((d for d in DEALS if d["deal_id"] == deal_id), {})
    out = {"product_name": name or deal.get("product_name", "?"), "quantity_estimate": "1"}
    if deal_id is not None:
        out["deal_id"] = deal_id
    return out


def chef_reply(groups):
    return json.dumps({"ingredient_groups": groups, "ingredient_reuse_map": {}})


def offered_in(prompt):
    """Deal rows the prompt offers (its Assigned Deals JSON block)."""
    start = prompt.index(ASSIGNED) + len(ASSIGNED)
    body = prompt[start:].lstrip()
    return json.JSONDecoder().raw_decode(body)[0]


def recipe_citing(deals, tag=""):
    return {"name": "Dish " + "+".join(str(d["deal_id"]) for d in deals) + tag,
            "meal_type": "dinner", "servings": 2,
            "ingredients": [{"name": d["product_name"], "deal_id": d["deal_id"],
                             "quantity": "1", "unit": "unit"} for d in deals],
            "instructions": ["Cook it."]}


def sous_reply(extra=0, short=0):
    """Answer with exactly target (+extra, -short) recipes citing every
    offered deal; a retry gets one recipe."""
    def respond(prompt):
        deals = offered_in(prompt)
        if RETRY_MARKER in prompt:
            return json.dumps({"recipes": [recipe_citing(deals, " v2")]})
        n = int(_TARGET.search(prompt).group(1)) + extra - short
        return json.dumps({"recipes": [recipe_citing(deals, f" #{i}") for i in range(n)]})
    return respond


def make_deps(groups, sous=None, deals=DEALS):
    return GraphDeps(
        chef_llm=ScriptedChatModel(respond=lambda p: chef_reply(groups), prompts=[]),
        sous_chef_llm=ScriptedChatModel(respond=sous or sous_reply(), prompts=[]),
        nutritionist_llm=ScriptedChatModel(respond=lambda p: NUTRITIONIST, prompts=[]),
        deals=FakeDeals(deals),
        catalog=FakeCatalog(STAPLES, {}),
        recipe_store=InMemoryRecipeStore(),
        tracker=RecordingTracker(),
    )


def run(deps, num_meals=3, budget=100.0, spent=0.0, restrictions=(), preferences=None,
        progress=None):
    return main_recipe_generation.run_recipe_generation(
        user_id=1, postal_code="M5V3A8", budget=budget, household_size=2,
        dietary_restrictions=list(restrictions), num_meals=num_meals,
        preferences=preferences, budget_period="weekly",
        budget_spent_this_period=spent, graph_deps=deps, progress_callback=progress)


def plan(state):
    return [state["generated_recipes"][rid] for rid in state["approved_recipe_ids"]]


THREE_GROUPS = [[_sel(1), _sel(2)], [_sel(3), _sel(4)], [_sel(5), _sel(6)]]


# --------------------------------------------------------------------------
# 1. Cost semantics: per unique deal
# --------------------------------------------------------------------------

def test_price_plan_charges_a_deal_reused_across_recipes_once():
    lookup = deals_by_id(DEALS)
    a = {"ingredients": [{"deal_id": 1}, {"deal_id": 2}, {"product_id": 101}]}
    b = {"ingredients": [{"deal_id": 1}, {"deal_id": 3}]}
    assert price_recipe(a["ingredients"], lookup).total_cost == 5.0
    assert price_recipe(b["ingredients"], lookup).total_cost == 8.0
    assert price_plan([a, b], lookup).total_cost == 10.0  # tofu bought once


def test_graph_total_cost_is_the_shopping_list_cost():
    """Deal 1 (tofu) is in all three groups, so all three recipes cite it;
    the plan charges it once. Each recipe keeps its own informational cost."""
    groups = [[_sel(1), _sel(2)], [_sel(1), _sel(3)], [_sel(1), _sel(5)]]
    state = run(make_deps(groups))
    assert state["status"] == "completed", state["errors"]
    recipes = plan(state)
    assert sorted(r["total_cost"] for r in recipes) == [4.0, 5.0, 8.0]  # bought alone
    assert state["total_cost"] == pytest.approx(3.0 + 2.0 + 5.0 + 1.0)
    assert state["cost_per_meal"] == pytest.approx(11.0 / 3)
    assert state["budget_remaining"] == pytest.approx(100.0 - 11.0)

    response = route._build_generation_response(state, 0.1)
    assert float(response.total_cost) == pytest.approx(11.0)
    assert sorted(float(r.total_cost) for r in response.recipes) == [4.0, 5.0, 8.0]


# --------------------------------------------------------------------------
# 2. Budget checked after generation
# --------------------------------------------------------------------------

def test_over_budget_plan_is_returned_with_an_over_status_and_a_warning():
    state = run(make_deps(THREE_GROUPS), budget=20.0, spent=10.0)  # 10 left; plan 13.75
    assert state["status"] == "completed"
    check = state["budget_check"]
    assert check["status"] == "over"
    assert check["plan_cost"] == pytest.approx(13.75)
    assert check["available"] == pytest.approx(10.0)
    assert check["headroom"] == pytest.approx(-3.75)
    assert check["lower_bound"] is True and check["caveats"]
    assert any(w.startswith("Over budget") and "at least $13.75" in w
               for w in state["warnings"])

    body = route._build_generation_response(state, 0.1).model_dump()
    assert body["budget_check"]["status"] == "over"
    assert body["status"] == "completed" and body["partial"] is False
    assert body["meals_requested"] == 3


def test_a_plan_under_the_budget_is_only_within_the_lower_bound():
    state = run(make_deps(THREE_GROUPS), budget=100.0)
    check = state["budget_check"]
    assert check["status"] == "within_lower_bound"   # never a bare "within"
    assert check["lower_bound"] is True
    assert check["headroom"] == pytest.approx(100.0 - 13.75)
    assert not any("budget" in w.lower() for w in state["warnings"])


def test_plan_budget_check_counts_period_spend():
    state = {"budget": 50.0, "budget_spent_this_period": 45.0, "budget_period": "weekly"}
    check = graph_mod.plan_budget_check(state, 6.0)
    assert check["status"] == "over" and check["available"] == 5.0
    assert graph_mod.plan_budget_check(state, 5.0)["status"] == "within_lower_bound"


# --------------------------------------------------------------------------
# 3. Recipe count
# --------------------------------------------------------------------------

def test_split_recipe_count_never_assigns_zero():
    assert graph_mod.split_recipe_count(7, 3) == [3, 2, 2]
    assert graph_mod.split_recipe_count(3, 3) == [1, 1, 1]
    assert graph_mod.split_recipe_count(2, 3) == [1, 1]
    assert graph_mod.split_recipe_count(1, 3) == [1]
    assert graph_mod.split_recipe_count(0, 3) == []


def test_merge_groups_folds_the_chef_plan_into_num_meals_groups():
    groups = [[_sel(1), _sel(2)], [_sel(3), _sel(1)], [_sel(5)]]
    assert _merge_groups(groups, 3) is groups
    assert _merge_groups(groups, 7) is groups
    two = _merge_groups(groups, 2)
    assert [[s["deal_id"] for s in g] for g in two] == [[1, 2, 5], [3, 1]]
    one = _merge_groups(groups, 1)
    assert [[s["deal_id"] for s in g] for g in one] == [[1, 2, 3, 5]]  # deduped


@pytest.mark.parametrize("num_meals,targets", [
    (1, [1]), (2, [1, 1]), (3, [1, 1, 1]), (7, [3, 2, 2]),
])
def test_num_meals_runs_no_sous_chef_with_target_zero_and_returns_exactly_n(
        num_meals, targets):
    deps = make_deps(THREE_GROUPS)
    nodes = []
    state = run(deps, num_meals=num_meals, progress=nodes.append)
    assert state["status"] == "completed", state["errors"]

    prompts = deps.sous_chef_llm.prompts
    assert nodes.count("sous_chef_generate") == len(targets)
    assert sorted(int(_TARGET.search(p).group(1)) for p in prompts) == sorted(targets)
    # Every planned deal stays on offer, however few SousChefs run.
    offered = {d["deal_id"] for p in prompts for d in offered_in(p)}
    assert offered == {1, 2, 3, 4, 5, 6}

    assert len(state["approved_recipe_ids"]) == num_meals
    assert len(state["saved_recipe_ids"]) == num_meals
    assert state["iteration_count"] == 0
    body = route._build_generation_response(state, 0.1).model_dump()
    assert len(body["recipes"]) == num_meals and body["partial"] is False


def test_surplus_recipes_are_trimmed_to_the_requested_count():
    deps = make_deps(THREE_GROUPS, sous=sous_reply(extra=1))
    state = run(deps, num_meals=3)
    assert len(state["generated_recipes"]) == 6
    assert len(state["approved_recipe_ids"]) == 3
    assert [s["recipe"]["recipe_id"] for s in deps.recipe_store.saved] == \
        state["approved_recipe_ids"]


def test_select_plan_keeps_the_cheapest_subset_in_approval_order():
    lookup = deals_by_id(DEALS)
    recipes = {
        "a": {"ingredients": [{"deal_id": 3}]},                    # 5.00
        "b": {"ingredients": [{"deal_id": 1}]},                    # 3.00
        "c": {"ingredients": [{"deal_id": 1}, {"deal_id": 5}]},    # reuses tofu
        "d": {"ingredients": [{"deal_id": 5}]},                    # 1.00
    }
    assert graph_mod.select_plan(["a", "b", "c", "d"], recipes, 2, lookup) == ["b", "c"]
    assert graph_mod.select_plan(["a", "b"], recipes, 3, lookup) == ["a", "b"]


def test_a_shortfall_is_topped_up_on_retry():
    """SousChef 2 (deals 3+4) answers nothing on its first call; the retry
    asks the SousChefs for the missing recipe and the plan reaches 3."""
    calls = {"group2": 0}
    lock = threading.Lock()
    base = sous_reply()

    def respond(prompt):
        if {d["deal_id"] for d in offered_in(prompt)} == {3, 4}:
            with lock:
                calls["group2"] += 1
                if calls["group2"] <= 3:          # all of invoke_validated's attempts
                    return "not JSON"
        return base(prompt)

    deps = make_deps(THREE_GROUPS, sous=respond)
    nodes = []
    state = run(deps, num_meals=3, progress=nodes.append)
    assert state["status"] == "completed", state["errors"]
    assert state["iteration_count"] == 1
    assert "retry_generation" in nodes
    assert len(state["approved_recipe_ids"]) == 3
    assert not any(w.startswith("Only") for w in state["warnings"])


def test_fewer_approved_than_requested_is_reported_as_partial():
    """Only two model calls ever succeed: the plan settles for 2 of 3 after
    both retry iterations, and the response says so."""
    budget = {"ok": 2}
    lock = threading.Lock()
    base = sous_reply()

    def respond(prompt):
        with lock:
            if budget["ok"] <= 0:
                return "not JSON"
            budget["ok"] -= 1
        return base(prompt)

    state = run(make_deps(THREE_GROUPS, sous=respond), num_meals=3)
    assert state["status"] == "completed"
    assert state["iteration_count"] == 2
    assert len(state["approved_recipe_ids"]) == 2
    assert "Only 2/3 recipes approved" in state["warnings"]
    body = route._build_generation_response(state, 0.1).model_dump()
    assert body["partial"] is True and body["meals_requested"] == 3
    assert len(body["recipes"]) == 2


def _route_state(approved, rejected, iteration, num_meals=3):
    return {"approved_recipe_ids": [f"a{i}" for i in range(approved)],
            "rejected_recipe_ids": [f"r{i}" for i in range(rejected)],
            "iteration_count": iteration, "max_iterations": 2, "num_meals": num_meals}


@pytest.mark.parametrize("approved,rejected,iteration,expected", [
    (3, 0, 0, "finalize"),
    (3, 1, 0, "finalize"),            # enough approved: no wasted retry
    (2, 1, 0, "retry"),
    (2, 0, 0, "retry"),               # shortfall, no rejection
    (0, 0, 0, "retry"),               # nothing yet, iterations left
    (2, 1, 2, "finalize_partial"),
    (1, 2, 2, "handle_failure"),
    (0, 0, 2, "handle_failure"),
])
def test_route_after_validation(approved, rejected, iteration, expected):
    assert graph_mod.route_after_validation(
        _route_state(approved, rejected, iteration)) == expected


# --------------------------------------------------------------------------
# 4. deal_index keyed by deal_id; no name resolution
# --------------------------------------------------------------------------

def test_deal_index_is_keyed_by_deal_id_and_keeps_same_named_deals_apart():
    chef = ChefOrchestrator(llm=object(), db=FakeDeals(DEALS), tracker=RecordingTracker())
    update = chef.initialize({"user_id": 1, "num_meals": 3, "budget": 50.0,
                              "dietary_restrictions": [], "postal_code": "M5V3A8"})
    index = update["deal_index"]
    assert set(index) == {"1", "2", "3", "4", "5", "6"}
    assert index["2"]["store_name"] == "FreshCo" and index["4"]["store_name"] == "No Frills"


def test_same_product_name_at_two_stores_reaches_the_plan_as_two_deals():
    groups = [[_sel(2), _sel(4)], [_sel(1)], [_sel(3)]]
    deps = make_deps(groups)
    state = run(deps, num_meals=3)
    broccoli = next(r for r in plan(state)
                    if {i["deal_id"] for i in r["ingredients"]} == {2, 4})
    assert broccoli["total_cost"] == pytest.approx(3.50)   # both deals priced


def test_a_chef_selection_without_deal_id_is_not_resolved_by_name():
    """The Chef names "FIRM TOFU" (an exact served name) with no deal_id in
    group 1: it is dropped, so no SousChef is offered deal 1 through it."""
    groups = [[_sel(None, "FIRM TOFU"), _sel(2)], [_sel(3)], [_sel(5)]]
    deps = make_deps(groups)
    state = run(deps, num_meals=3)
    assert state["status"] == "completed"
    assert state["ingredient_groups"][0] == [
        {"product_name": "BROCCOLI CROWNS", "quantity_estimate": "1", "deal_id": 2}]
    offered = {d["deal_id"] for p in deps.sous_chef_llm.prompts for d in offered_in(p)}
    assert 1 not in offered


# --------------------------------------------------------------------------
# 5. Preferences reach the SousChefs
# --------------------------------------------------------------------------

def test_preferences_reach_every_sous_chef_prompt():
    prefs = {"avoid_ingredients": ["mushrooms"], "cuisine_preferences": ["Thai"]}
    deps = make_deps(THREE_GROUPS)
    run(deps, num_meals=3, preferences=prefs)
    assert len(deps.sous_chef_llm.prompts) == 3
    for prompt in deps.sous_chef_llm.prompts:
        assert '"avoid_ingredients": ["mushrooms"]' in prompt and "Thai" in prompt
    assert "mushrooms" in deps.chef_llm.prompts[0]


def test_preferences_reach_the_retry_prompt():
    llm = ScriptedChatModel(respond=sous_reply(), prompts=[])
    chef = SousChef(llm=llm, tracker=RecordingTracker())
    recipe = chef.regenerate_with_feedback(
        chef_id="sous_chef_1", original_recipe={"name": "x"}, feedback="drop it",
        ingredient_group=[_sel(1)], household_size=2, dietary_restrictions=[],
        available_deals=DEALS, pantry_staples=STAPLES,
        preferences={"avoid_ingredients": ["cilantro"]})
    assert recipe is not None
    assert RETRY_MARKER in llm.prompts[0] and "cilantro" in llm.prompts[0]


def test_no_preferences_render_as_none():
    llm = ScriptedChatModel(respond=sous_reply(), prompts=[])
    SousChef(llm=llm, tracker=RecordingTracker()).generate_recipes(
        chef_id="sous_chef_1", ingredient_group=[_sel(1)], target_recipe_count=1,
        household_size=2, dietary_restrictions=[], available_deals=DEALS,
        pantry_staples=STAPLES)
    assert "- Preferences: none" in llm.prompts[0]


# --------------------------------------------------------------------------
# 6. Retry strategies
# --------------------------------------------------------------------------

def _rejection_state(iteration, groups=3):
    return {
        "iteration_count": iteration, "num_meals": 3,
        "approved_recipe_ids": ["a"], "rejected_recipe_ids": ["r"],
        "validation_results": {"r": {"feedback": "cites deal 3: contains_meat"}},
    }


def test_handle_rejections_strategy_per_iteration():
    chef = ChefOrchestrator(llm=object(), db=object(), tracker=RecordingTracker())
    first = chef.handle_rejections(_rejection_state(0))
    assert first["retry_strategy"] == "feedback" and first["iteration_count"] == 1
    assert first["recipes_pending_retry"] == {"r": "cites deal 3: contains_meat"}
    assert first["recipes_shortfall"] == 1           # 3 wanted, 1 approved, 1 pending
    second = chef.handle_rejections(_rejection_state(1))
    assert second["retry_strategy"] == "reassign_chef" and second["iteration_count"] == 2


class StubSousChef:
    """Records which SousChef / group each retry call went to."""

    def __init__(self):
        self.regenerated, self.generated = [], []

    def regenerate_with_feedback(self, chef_id, original_recipe, feedback,
                                 ingredient_group, **kw):
        self.regenerated.append((chef_id, [s["deal_id"] for s in ingredient_group],
                                 feedback, kw["preferences"]))
        return {"recipe_id": f"new-{original_recipe['recipe_id']}"}

    def generate_recipes(self, chef_id, ingredient_group, target_recipe_count, **kw):
        self.generated.append((chef_id, target_recipe_count))
        return [{"recipe_id": f"{chef_id}-{i}"} for i in range(target_recipe_count)]


def _retry_state(strategy, groups=THREE_GROUPS, shortfall=0):
    return {
        "retry_strategy": strategy, "recipes_shortfall": shortfall,
        "recipes_pending_retry": {"r": "drop deal 3"},
        "generated_recipes": {"r": {"recipe_id": "r"}},
        "sous_chef_assignments": {"r": "sous_chef_2"},
        "rejected_recipe_ids": ["r"],
        "ingredient_groups": groups, "household_size": 2, "dietary_restrictions": [],
        "available_deals": DEALS, "pantry_staples": [], "deal_index": deals_by_id(DEALS),
        "preferences": {"avoid_ingredients": ["mushrooms"]},
    }


def test_feedback_strategy_keeps_the_same_sous_chef_and_group():
    stub = StubSousChef()
    update = graph_mod.retry_generation(_retry_state("feedback"), sous_chef=stub)
    assert stub.regenerated == [("sous_chef_2", [3, 4], "drop deal 3",
                                 {"avoid_ingredients": ["mushrooms"]})]
    assert update["sous_chef_assignments"] == {"new-r": "sous_chef_2"}
    assert update["rejected_recipe_ids"] == []


def test_reassign_strategy_hands_the_recipe_to_the_next_sous_chef():
    stub = StubSousChef()
    update = graph_mod.retry_generation(_retry_state("reassign_chef"), sous_chef=stub)
    assert stub.regenerated[0][:2] == ("sous_chef_3", [5, 6])
    assert update["sous_chef_assignments"] == {"new-r": "sous_chef_3"}
    # ...and round-robin wraps.
    state = _retry_state("reassign_chef")
    state["sous_chef_assignments"] = {"r": "sous_chef_3"}
    stub = StubSousChef()
    graph_mod.retry_generation(state, sous_chef=stub)
    assert stub.regenerated[0][:2] == ("sous_chef_1", [1, 2])


def test_reassign_with_a_single_group_degrades_to_feedback():
    stub = StubSousChef()
    state = _retry_state("reassign_chef", groups=[[_sel(1), _sel(3)]])
    state["sous_chef_assignments"] = {"r": "sous_chef_1"}
    graph_mod.retry_generation(state, sous_chef=stub)
    assert stub.regenerated[0][:2] == ("sous_chef_1", [1, 3])


def test_retry_tops_up_the_shortfall_across_groups():
    stub = StubSousChef()
    update = graph_mod.retry_generation(_retry_state("feedback", shortfall=4),
                                        sous_chef=stub)
    assert stub.generated == [("sous_chef_1", 2), ("sous_chef_2", 1), ("sous_chef_3", 1)]
    assert len(update["generated_recipes"]) == 1 + 4
    assert update["recipes_shortfall"] == 0


# --------------------------------------------------------------------------
# 7. Combination-rule (kosher) prompt hints
# --------------------------------------------------------------------------

def test_kosher_hint_is_derived_from_the_combination_rules():
    (hint,) = combination_rule_hints(["Kosher"])
    assert hint.startswith("kosher:") and "meat and dairy" in hint
    assert "same recipe" in hint
    assert combination_rule_hints(["vegan", "vegetarian"]) == []
    assert combination_rule_hints([]) == []


def test_a_new_combination_rule_gets_a_hint_without_a_prompt_edit(monkeypatch):
    monkeypatch.setitem(products.RESTRICTION_COMBINATION_RULES, "vegan",
                        (("contains_nuts", "contains_egg", "contains_fish"),))
    (hint,) = combination_rule_hints(["vegan"], unit="group")
    assert "nuts, egg and fish in the same group" in hint


@pytest.mark.parametrize("restrictions,hinted", [
    (["kosher"], True), (["vegetarian"], False), ([], False),
])
def test_kosher_hint_reaches_chef_and_sous_chef_prompts_only_for_kosher(
        restrictions, hinted):
    llm = ScriptedChatModel(respond=sous_reply(), prompts=[])
    sous = SousChef(llm=llm, tracker=RecordingTracker())
    sous.generate_recipes(
        chef_id="sous_chef_1", ingredient_group=[_sel(1)], target_recipe_count=1,
        household_size=2, dietary_restrictions=restrictions, available_deals=DEALS,
        pantry_staples=STAPLES)
    sous.regenerate_with_feedback(
        chef_id="sous_chef_1", original_recipe={"name": "x"}, feedback="f",
        ingredient_group=[_sel(1)], household_size=2, dietary_restrictions=restrictions,
        available_deals=DEALS, pantry_staples=STAPLES)

    chef_llm = ScriptedChatModel(respond=lambda p: chef_reply(THREE_GROUPS), prompts=[])
    chef = ChefOrchestrator(llm=chef_llm, db=object(), tracker=RecordingTracker())
    chef.plan_ingredient_groups({
        "available_deals": DEALS, "budget": 50.0, "household_size": 2, "num_meals": 3,
        "dietary_restrictions": restrictions, "preferences": {}})

    assert len(llm.prompts) == 2 and len(chef_llm.prompts) == 1
    for prompt in llm.prompts:
        assert ("never combine meat and dairy in the same recipe" in prompt) is hinted
    assert ("never combine meat and dairy in the same group" in chef_llm.prompts[0]) is hinted
