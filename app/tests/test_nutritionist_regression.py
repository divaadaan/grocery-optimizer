"""Regression suite for observed dietary-compliance failures (live LLM).

Fixtures: fixtures/nutritionist_cases.json, converted from
nutritionist_regression_cases.txt (2026-06-12 runs). Every REJECT case is a
recipe the Nutritionist correctly rejected then — if a model/prompt change
makes it approve one, that is a regression. The chef-grouping test is the
known-failing SFT target (qwen2.5:7b puts meat in groups for vegetarians).

Runs against the live Ollama models from .env:  pytest -m llm
Skipped automatically when Ollama is unreachable.
"""
import json
import pathlib

import pytest

pytestmark = pytest.mark.llm

FIXTURES = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "nutritionist_cases.json").read_text(encoding="utf-8")
)
PROFILE = FIXTURES["user_profile"]
FULL_CASES = [c for c in FIXTURES["nutritionist_cases"] if c["recipe"] is not None]
NAME_ONLY_CASES = [c for c in FIXTURES["nutritionist_cases"] if c["recipe"] is None]

# Both REJECT and APPROVE cases are now hard regression guards. The APPROVE
# controls were xfail while the verdict came from the LLM (phi4-mini and every
# other model false-reject minimal compliant recipes and confabulate violations;
# ROADMAP 2026-07-15). Dietary compliance is now decided deterministically in
# app/agents/dietary.py, so the verdict no longer depends on the model — these
# assertions must hold every run.
VERDICT_PARAMS = [pytest.param(c, id=c["id"]) for c in FULL_CASES]


@pytest.fixture(scope="module", autouse=True)
def require_ollama():
    import requests
    from app.config import settings
    try:
        requests.get(settings.ollama_base_url, timeout=3)
    except requests.RequestException:
        pytest.skip(f"Ollama not reachable at {settings.ollama_base_url}")


def _validate_one(recipe: dict, dietary_restrictions: list) -> dict:
    """Run a single recipe through Nutritionist.validate_recipes."""
    from app.agents.nutritionist import Nutritionist

    recipe = {**recipe, "recipe_id": "case-under-test"}
    state = {
        "generated_recipes": {"case-under-test": recipe},
        "validation_results": {},
        "dietary_restrictions": dietary_restrictions,
        "approved_recipe_ids": [],
        "rejected_recipe_ids": [],
    }
    update = Nutritionist().validate_recipes(state)
    return update["validation_results"]["case-under-test"]


@pytest.mark.parametrize("case", VERDICT_PARAMS)
def test_nutritionist_verdict(case):
    result = _validate_one(case["recipe"], PROFILE["dietary_restrictions"])
    assert result["approved"] == case["expected_approved"], (
        f"{case['id']}: expected approved={case['expected_approved']} "
        f"(violation: {case['violation']}); feedback: {result['feedback']}"
    )


@pytest.mark.parametrize("case", NAME_ONLY_CASES, ids=[c["id"] for c in NAME_ONLY_CASES])
def test_name_only_cases_documented(case):
    pytest.skip(f"name-only case, full recipe JSON not captured: {case['note']}")


@pytest.mark.xfail(
    reason="known failure: qwen2.5:7b chef ignores vegetarian restriction when "
    "grouping (NEXT_STEPS item 3) — this is the SFT acceptance bar",
    strict=False,
)
def test_chef_groups_respect_vegetarian_restriction():
    from app.agents.chef_orchestrator import ChefOrchestrator

    grouping = FIXTURES["chef_grouping"]
    forbidden = grouping["forbidden_terms_vegetarian"]

    state = {
        "available_deals": grouping["deals"],
        "budget": PROFILE["budget"],
        "household_size": PROFILE["household_size"],
        "num_meals": PROFILE["num_meals"],
        "dietary_restrictions": PROFILE["dietary_restrictions"],
        "preferences": {},
    }
    update = ChefOrchestrator().plan_ingredient_groups(state)
    assert "ingredient_groups" in update, f"chef planning failed: {update.get('errors')}"

    violations = [
        item["product_name"]
        for group in update["ingredient_groups"]
        for item in group
        if any(term in item["product_name"].casefold() for term in forbidden)
    ]
    assert not violations, f"meat/poultry/fish assigned to a vegetarian user: {violations}"
