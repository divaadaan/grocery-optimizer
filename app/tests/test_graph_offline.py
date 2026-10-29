"""The real compiled recipe graph, end to end, with no Ollama and no database.

Everything the graph reaches outside itself comes in through ``GraphDeps``
(``app/agents/deps.py``): scripted chat models answer from a recorded
fixture (``fixtures/graph_run_vegetarian.json``) in shapes the real
``llm_output`` parsers accept, and an in-memory catalog / deal source /
recipe store stands in for Postgres. Every node, edge, reducer, the Send()
fan-out, the retry loop and the deterministic dietary verdict are the
production code.

Runs go through ``run_recipe_generation`` -- the same
``graph.stream(stream_mode=["updates", "values"])`` call the API route makes
-- and one test drives the route's own worker (``_run_generation_job``), so
the progress-stage contract is checked against the real graph's events
rather than a stub (ROADMAP Next §2 caveat).

Guards: any attempt to open a database connection or configure MLflow fails
the test, so a dependency that bypasses injection cannot hide.
"""
import copy
import json
import re
from pathlib import Path
from typing import Any, Callable, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app import main_recipe_generation
from app.agents import graph as graph_mod
from app.agents.deps import GraphDeps, InMemoryRecipeStore
from app.agents.dietary import CitationCatalog, compliance_report
from app.services.shopping_optimizer import optimize_shopping_list

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "graph_run_vegetarian.json").read_text("utf-8")
)

RETRY_MARKER = "revising a rejected recipe"  # SOUS_CHEF_RETRY_WITH_FEEDBACK
_DEAL_ID = re.compile(r'"deal_id":\s*(\d+)')


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class ScriptedChatModel(BaseChatModel):
    """A real langchain chat model whose reply is computed from the prompt.

    ``respond`` gets the first (task) message's text and returns the raw
    string the model "emits"; it goes through invoke_validated's JSON +
    pydantic validation exactly as an Ollama reply would. The three
    SousChef tasks run in parallel, so replies are chosen by what the prompt
    offers, never by call order.
    """

    respond: Callable[[str], str]
    prompts: List[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        prompt = messages[0].content
        self.prompts.append(prompt)
        content = self.respond(prompt)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])


def _sous_chef_reply(prompt: str) -> str:
    offered = {int(i) for i in _DEAL_ID.findall(prompt)}
    script = FIXTURE["llm"]
    if RETRY_MARKER in prompt:
        entries = [script["sous_chef_retry"]]
    else:
        entries = script["sous_chef"]
    # Subset, not equality: the prompt's output-format example cites a
    # placeholder deal_id too. The three groups are disjoint.
    for entry in entries:
        if set(entry["when_offered"]) <= offered:
            return json.dumps(entry["response"])
    raise AssertionError(f"no scripted SousChef reply for offered deals {sorted(offered)}")


class FakeDeals:
    """``deals`` dependency: the post-gate deal set for the user."""

    def __init__(self, deals):
        self.deals = deals
        self.calls = []

    def fetch_current_deals(self, postal_code, dietary_restrictions=None):
        self.calls.append((postal_code, dietary_restrictions))
        return copy.deepcopy(self.deals)


class FakeCatalog:
    """``catalog`` dependency: what recipe_catalog would read."""

    def __init__(self, staples, deal_products):
        self.staples = staples
        self.deal_products = deal_products

    def fetch_pantry_staples(self):
        return copy.deepcopy(self.staples)

    def fetch_deal_products(self, deal_ids):
        wanted = {str(i) for i in deal_ids}
        return {k: copy.deepcopy(v) for k, v in self.deal_products.items() if k in wanted}


class RecordingTracker:
    """``tracker`` dependency: MLflowLogger's interface, recorded, no I/O."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return "" if name == "start_run" else None
        return record


def make_deps(deal_products=None) -> GraphDeps:
    script = FIXTURE["llm"]
    return GraphDeps(
        chef_llm=ScriptedChatModel(respond=lambda p: json.dumps(script["chef"]), prompts=[]),
        sous_chef_llm=ScriptedChatModel(respond=_sous_chef_reply, prompts=[]),
        nutritionist_llm=ScriptedChatModel(
            respond=lambda p: json.dumps(script["nutritionist"]), prompts=[]),
        deals=FakeDeals(FIXTURE["deals"]),
        catalog=FakeCatalog(FIXTURE["pantry_staples"],
                            deal_products or FIXTURE["deal_products"]),
        recipe_store=InMemoryRecipeStore(),
        tracker=RecordingTracker(),
    )


@pytest.fixture(autouse=True)
def no_database_no_mlflow(monkeypatch):
    """Anything that bypasses GraphDeps and reaches Postgres or MLflow fails."""
    from app.db.database import Database
    from app.services import mlflow_logger

    def no_db(*args, **kwargs):
        raise AssertionError("offline graph test touched the database")

    def no_mlflow():
        raise AssertionError("offline graph test touched MLflow")

    monkeypatch.setattr(Database, "initialize", no_db)
    monkeypatch.setattr(Database, "get_connection", no_db)
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", no_mlflow)


def _run(deps, progress=None, **overrides):
    user = {**FIXTURE["user"], **overrides}
    return main_recipe_generation.run_recipe_generation(
        user_id=user["user_id"],
        postal_code=user["postal_code"],
        budget=user["budget"],
        household_size=user["household_size"],
        dietary_restrictions=user["dietary_restrictions"],
        num_meals=3,
        progress_callback=progress,
        budget_period="week",
        budget_spent_this_period=10.0,
        graph_deps=deps,
    )


def _approved(state):
    return [state["generated_recipes"][rid] for rid in state["approved_recipe_ids"]]


SERVED_DEAL_IDS = {d["deal_id"] for d in FIXTURE["deals"]}
DEAL_PRICE = {d["deal_id"]: d["sale_price"] for d in FIXTURE["deals"]}


def _offered_staple_ids(restrictions):
    from app.agents.sous_chef import offered_staples
    return {s["product_id"] for s in offered_staples(FIXTURE["pantry_staples"], restrictions)}


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_real_graph_produces_a_cited_compliant_costed_meal_plan():
    deps = make_deps()
    nodes = []
    state = _run(deps, progress=nodes.append)

    assert state["status"] == "completed", state["errors"]
    assert state["iteration_count"] == 0

    # The deal source was asked for the user's gated deals.
    assert deps.deals.calls == [("M5V3A8", ["vegetarian"])]

    # Recipes: one per SousChef, all approved.
    recipes = _approved(state)
    assert sorted(r["name"] for r in recipes) == [
        "Chickpea Dumpling Bowl", "Tofu and Broccoli Stir-Fry", "Tomato Rice Pilaf"]
    assert state["rejected_recipe_ids"] == []

    # Every ingredient cites exactly one served deal or an offered staple.
    staple_ids = _offered_staple_ids(["vegetarian"])
    assert staple_ids == {101, 102}  # fish sauce never offered to a vegetarian
    for recipe in recipes:
        for ing in recipe["ingredients"]:
            cites = [k for k in ("deal_id", "product_id") if ing.get(k) is not None]
            assert len(cites) == 1, ing
            if "deal_id" in cites:
                assert ing["deal_id"] in SERVED_DEAL_IDS
            else:
                assert ing["product_id"] in staple_ids
    for prompt in deps.sous_chef_llm.prompts:
        assert '"product_id": 103' not in prompt

    # The dietary verdict is the deterministic citation lookup, not the LLM
    # (whose scripted verdict is "approved": false for everything).
    catalog = CitationCatalog.from_state(state)
    for rid in state["approved_recipe_ids"]:
        result = state["validation_results"][rid]
        recipe = state["generated_recipes"][rid]
        expected = compliance_report(recipe, ["vegetarian"], catalog)
        assert result["approved"] is True and expected["approved"] is True
        assert result["dietary_compliance"]["violations"] == expected["violations"] == []
        # Advisory facts still flow through from the (scripted) Nutritionist.
        assert result["nutrition_facts"]["calories_per_serving"] == 520
        assert result["health_score"] == 72.0

    # Costs come from citations: sale prices of cited deals, staples free.
    expected_total = sum(DEAL_PRICE[i["deal_id"]] for r in recipes
                         for i in r["ingredients"] if i.get("deal_id") is not None)
    assert state["total_cost"] == pytest.approx(expected_total)
    assert state["budget_remaining"] == pytest.approx(60.0 - 10.0 - expected_total)
    # Savings from the two deals with a regular price (tofu, rice).
    assert state["estimated_savings"] == pytest.approx((3.29 - 2.49) + (8.99 - 6.99))

    # Persisted through the injected store, ids aligned with approval order.
    saved = deps.recipe_store.saved
    assert state["saved_recipe_ids"] == [s["id"] for s in saved] == [1, 2, 3]
    assert [s["recipe"]["recipe_id"] for s in saved] == state["approved_recipe_ids"]
    assert {s["user_id"] for s in saved} == {4242}

    # A shopping list builds from the produced recipes.
    shopping = optimize_shopping_list(recipes, FIXTURE["deals"])
    assert {i["product"] for i in shopping.items} == {
        d["product_name"] for d in FIXTURE["deals"]}  # every served deal cited once
    assert shopping.total_cost == pytest.approx(expected_total)
    assert shopping.stores == ["FreshCo", "No Frills"]

    # Streamed node order (the route's progress source).
    assert nodes[:3] == ["initialize_chef", "load_recipe_catalog", "plan_ingredient_groups"]
    assert nodes[3:6] == ["sous_chef_generate"] * 3
    assert nodes[6:] == ["aggregate_recipes", "nutritionist_validate", "finalize_meal_plan"]

    # MLflow went to the injected tracker only.
    assert deps.tracker.calls[0] == "start_run"
    assert deps.tracker.calls[-1] == "finalize_run"


# --------------------------------------------------------------------------
# Rejection -> retry loop
# --------------------------------------------------------------------------

def test_real_graph_rejects_on_attributes_and_retries_with_feedback():
    """The catalog says deal 15 ("VEGGIE DUMPLINGS") contains meat. The
    recipe citing it is rejected by attribute -- its name is irrelevant --
    and the SousChef's regenerated recipe is approved on the second pass."""
    deal_products = {**FIXTURE["deal_products"], **FIXTURE["deal_products_rereviewed"]}
    deps = make_deps(deal_products=deal_products)
    nodes = []
    state = _run(deps, progress=nodes.append)

    assert state["status"] == "completed", state["errors"]
    assert state["iteration_count"] == 1
    assert "handle_rejections" in nodes and "retry_generation" in nodes
    assert nodes.count("nutritionist_validate") == 2

    rejected = [rid for rid, v in state["validation_results"].items() if not v["approved"]]
    assert len(rejected) == 1
    bad = state["generated_recipes"][rejected[0]]
    assert bad["name"] == "Chickpea Dumpling Bowl"
    violations = state["validation_results"][rejected[0]]["dietary_compliance"]["violations"]
    assert len(violations) == 1 and "contains_meat" in violations[0]

    # The feedback reached the retry prompt.
    retry_prompts = [p for p in deps.sous_chef_llm.prompts if RETRY_MARKER in p]
    assert len(retry_prompts) == 1
    assert "contains_meat" in retry_prompts[0]

    recipes = _approved(state)
    assert rejected[0] not in state["approved_recipe_ids"]
    assert "Spiced Chickpea Skillet" in {r["name"] for r in recipes}
    assert all(i.get("deal_id") != 15 for r in recipes for i in r["ingredients"])
    assert len(deps.recipe_store.saved) == 3


# --------------------------------------------------------------------------
# The API route's worker, on the real graph
# --------------------------------------------------------------------------

def test_route_worker_runs_the_real_graph_and_reports_real_stages(monkeypatch):
    from app.models.schemas import RecipeGenerationRequest
    from app.routes import recipes as route
    from app.services.recipe_jobs import COMPLETED, PIPELINE_STAGES, RecipeJobRegistry

    deps = make_deps()
    real_create = graph_mod.create_recipe_generation_graph
    monkeypatch.setattr(main_recipe_generation, "create_recipe_generation_graph",
                        lambda graph_deps=None: real_create(deps))
    monkeypatch.setattr(route, "budget_status",
                        lambda user: {"budget_period": "week", "spent": 10.0})

    registry = RecipeJobRegistry(max_workers=1)
    monkeypatch.setattr(route, "job_registry", registry)

    stages = []
    real_report = registry.report_node

    def report(job_id, node):
        before = registry.get(job_id).stage
        real_report(job_id, node)
        after = registry.get(job_id).stage
        if after != before:
            stages.append(after)

    monkeypatch.setattr(registry, "report_node", report)

    user = {**FIXTURE["user"]}
    job = registry.create(user_id=user["user_id"], num_meals=3)
    route._run_generation_job(
        job.job_id, user, RecipeGenerationRequest(user_id=user["user_id"], num_meals=3))

    assert job.state == COMPLETED, job.error
    assert stages == [
        PIPELINE_STAGES[1],
        "SousChef 1/3 cooking…",
        "SousChef 2/3 cooking…",
        "SousChef 3/3 cooking…",
        PIPELINE_STAGES[3],
        PIPELINE_STAGES[4],
        "Reviewing the Nutritionist's verdicts",
        PIPELINE_STAGES[5],
    ]

    result = job.result
    assert result["status"] == "completed"
    assert [r["recipe_id"] for r in result["recipes"]] == [1, 2, 3]
    assert all(r["nutrition_facts"]["calories_per_serving"] == 520 for r in result["recipes"])
    assert float(result["estimated_savings"]) == pytest.approx(2.8)  # Decimal in the schema
