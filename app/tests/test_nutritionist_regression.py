"""Regression suite for observed dietary-compliance failures.

Fixtures: fixtures/nutritionist_cases.json, converted from
nutritionist_regression_cases.txt (2026-06-12 runs). Every REJECT case is a
recipe the Nutritionist correctly rejected then — if a change makes it approve
one, that is a regression.

The verdict tests are deterministic: compliance is a lookup of each cited
deal's curated product attributes (app/agents/dietary.py), so they run without
a model. The advisory nutrition-facts LLM is replaced by a canned response.
The captured recipes were annotated with the deal_ids of the seed deals they
name (see the fixture's ``_citations`` note).

The chef-grouping test still needs live Ollama (``pytest -m llm``): it is the
known-failing SFT target (qwen2.5:7b puts meat in groups for vegetarians).
"""
import json
import pathlib

import pytest

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "nutritionist_cases.json"
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
PROFILE = FIXTURES["user_profile"]
FULL_CASES = [c for c in FIXTURES["nutritionist_cases"] if c["recipe"] is not None]
NAME_ONLY_CASES = [c for c in FIXTURES["nutritionist_cases"] if c["recipe"] is None]
PRODUCTS = {p["product_id"]: p for p in FIXTURES["products"]}
DEALS = FIXTURES["chef_grouping"]["deals"]
# What load_recipe_catalog would read from deal_products for the seed deals.
DEAL_PRODUCTS = {str(d["deal_id"]): [PRODUCTS[d["product_id"]]] for d in DEALS}

VERDICT_PARAMS = [pytest.param(c, id=c["id"]) for c in FULL_CASES]


@pytest.fixture(autouse=True)
def no_mlflow(monkeypatch):
    from app.services import mlflow_logger
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", lambda: False)


class _AdvisoryLLM:
    """Canned advisory nutrition facts; the verdict never depends on it."""

    def invoke(self, messages):
        class R:
            content = json.dumps({"approved": False,
                                  "nutrition_facts": {"calories_per_serving": 500},
                                  "health_score": 70})
        return R()


def _validate_one(recipe: dict, dietary_restrictions: list) -> dict:
    """Run a single recipe through Nutritionist.validate_recipes."""
    from app.agents.nutritionist import Nutritionist

    nutritionist = Nutritionist.__new__(Nutritionist)
    nutritionist.llm = _AdvisoryLLM()
    recipe = {**recipe, "recipe_id": "case-under-test"}
    state = {
        "generated_recipes": {"case-under-test": recipe},
        "validation_results": {},
        "dietary_restrictions": dietary_restrictions,
        "approved_recipe_ids": [],
        "rejected_recipe_ids": [],
        "available_deals": DEALS,
        "deal_products": DEAL_PRODUCTS,
        "pantry_staples": [],
    }
    update = nutritionist.validate_recipes(state)
    return update["validation_results"]["case-under-test"]


@pytest.mark.parametrize("case", VERDICT_PARAMS)
def test_nutritionist_verdict(case):
    result = _validate_one(case["recipe"], PROFILE["dietary_restrictions"])
    assert result["approved"] == case["expected_approved"], (
        f"{case['id']}: expected approved={case['expected_approved']} "
        f"(violation: {case['violation']}); feedback: {result['feedback']}"
    )


def test_chicken_case_names_the_display_name_and_the_attribute():
    case = next(c for c in FULL_CASES if c["id"] == "case1_chicken_for_vegetarian")
    result = _validate_one(case["recipe"], PROFILE["dietary_restrictions"])
    assert result["dietary_compliance"]["violations"] == [
        "Chicken Breast Boneless (deal 1): product 1 'Chicken Breast' has contains_meat"
    ]
    assert result["dietary_compliance"]["meets_restrictions"] is False
    assert result["dietary_compliance"]["allergen_free"] is True


@pytest.mark.parametrize("case", NAME_ONLY_CASES, ids=[c["id"] for c in NAME_ONLY_CASES])
def test_name_only_cases_documented(case):
    pytest.skip(f"name-only case, full recipe JSON not captured: {case['note']}")


@pytest.fixture
def require_ollama():
    import requests
    from app.config import settings
    try:
        requests.get(settings.ollama_base_url, timeout=3)
    except requests.RequestException:
        pytest.skip(f"Ollama not reachable at {settings.ollama_base_url}")


@pytest.mark.llm
def test_chef_groups_respect_vegetarian_restriction(require_ollama):
    """The Chef no longer filters by name: it trusts that fetch_current_deals
    already applied the products gate. So it is fed only the seed deals whose
    curated product clears the restriction -- what the gate would return --
    and must group only those deals. A selection outside the gated menu
    (hallucinated, or meat it was never shown) fails, judged by deal_id and
    product attributes rather than by name."""
    from app.agents.chef_orchestrator import ChefOrchestrator
    from app.services.products import product_allows

    restrictions = PROFILE["dietary_restrictions"]
    gated = [d for d in DEALS if product_allows(PRODUCTS[d["product_id"]], restrictions)]
    assert gated and len(gated) < len(DEALS)
    gated_ids = {str(d["deal_id"]) for d in gated}

    state = {
        "available_deals": gated,
        "budget": PROFILE["budget"],
        "household_size": PROFILE["household_size"],
        "num_meals": PROFILE["num_meals"],
        "dietary_restrictions": PROFILE["dietary_restrictions"],
        "preferences": {},
    }
    update = ChefOrchestrator().plan_ingredient_groups(state)
    assert "ingredient_groups" in update, f"chef planning failed: {update.get('errors')}"

    outside_menu = [
        (item.get("deal_id"), item.get("product_name"))
        for group in update["ingredient_groups"]
        for item in group
        if str(item.get("deal_id")) not in gated_ids
    ]
    assert not outside_menu, f"Chef selected deals outside the gated menu: {outside_menu}"
