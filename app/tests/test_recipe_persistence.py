"""Recipe persistence: one generation, its provenance, and everything a
recipe carries (Track C3, migration 022).

No database: ``DatabaseService`` runs against a fake cursor, and the graph
runs offline with the ``InMemoryRecipeStore`` (fixtures from
``test_graph_offline``).
"""
import contextlib
import copy
import json
import uuid

import pytest

from app.agents import graph
from app.agents.deps import InMemoryRecipeStore
from app.agents.llm_factory import ROLES, configured_models
from app.services.database import DatabaseService, _jsonable
from app.tests.test_graph_offline import _approved, _run, make_deps


# --------------------------------------------------------------------------
# DatabaseService.save_recipes against a fake cursor
# --------------------------------------------------------------------------

class FakeCursor:
    def __init__(self):
        self.executed = []
        self._next_id = 100

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))

    def fetchone(self):
        self._next_id += 1
        return {"recipe_id": self._next_id}


class FakeDb:
    def __init__(self):
        self.cursors = []

    @contextlib.contextmanager
    def get_cursor(self):
        cursor = FakeCursor()
        self.cursors.append(cursor)
        yield cursor


RECIPE = {
    "recipe_id": "r-1",
    "name": "Lentil Curry",
    "ingredients": [
        {"name": "lentils", "deal_id": 17, "quantity": "1", "unit": "cup", "price": 2.49},
        {"name": "salt", "product_id": 9, "quantity": "1", "unit": "tsp", "price": 0.0},
    ],
    "instructions": ["Simmer lentils", "Season"],
    "servings": 2,
    "total_cost": 2.49,
    "estimated_prep_time": 35,
    "meal_type": "dinner",
    "cuisine_type": "Indian",
    "nutrition_facts": {"calories": 480, "protein_g": 22},
    "health_score": 78.5,
    "dietary_compliance": {"allergen_free": True, "meets_restrictions": True,
                           "violations": []},
}

GENERATION = {
    "generation_id": "6f1c2e5b-7a4d-4f0e-8b3a-6c1d2e5f7a90",
    "status": "partial",
    "num_meals_requested": 3,
    "num_recipes_saved": 1,
    "dietary_restrictions": ["vegetarian"],
    "budget": 60.0,
    "budget_period": "weekly",
    "budget_spent_this_period": 10.0,
    "household_size": 2,
    "postal_code": "M5V3A8",
    "preferences": {"cuisine_preferences": ["Indian"]},
    "models": {"chef": {"provider": "ollama", "model": "big"}},
    "snapshot": None,
    "mlflow_run_id": "abc123",
    "total_cost": 2.49,
    "estimated_savings": None,
}


def _service():
    service = DatabaseService.__new__(DatabaseService)
    service.db = FakeDb()
    return service


def test_save_recipes_writes_generation_and_recipes_in_one_transaction():
    service = _service()
    ids = service.save_recipes(7, [RECIPE], generation=GENERATION)

    assert ids == [101]
    # One cursor == one connection == one transaction: the generation row and
    # its recipes commit or roll back together.
    assert len(service.db.cursors) == 1
    (gen_sql, gen_params), (recipe_sql, recipe_params) = service.db.cursors[0].executed

    assert gen_sql.startswith("INSERT INTO recipe_generations")
    assert gen_params["generation_id"] == GENERATION["generation_id"]
    assert gen_params["user_id"] == 7
    assert gen_params["status"] == "partial"
    assert gen_params["num_meals_requested"] == 3
    assert gen_params["num_recipes_saved"] == 1
    assert gen_params["dietary_restrictions"] == ["vegetarian"]
    assert gen_params["budget"] == 60.0
    assert gen_params["budget_period"] == "weekly"
    assert gen_params["budget_spent_this_period"] == 10.0
    assert json.loads(gen_params["models"]) == GENERATION["models"]
    assert gen_params["snapshot"] is None          # production: no snapshot
    assert json.loads(gen_params["preferences"]) == GENERATION["preferences"]
    assert gen_params["mlflow_run_id"] == "abc123"
    assert gen_params["estimated_savings"] is None  # unknown stays NULL

    assert recipe_sql.startswith("INSERT INTO recipes")
    (user_id, name, ingredients, instructions, total_cost, servings, prep_time,
     cuisine, meal_type, nutrition, compliance, health, generation_id) = recipe_params
    assert (user_id, name, servings, total_cost) == (7, "Lentil Curry", 2, 2.49)
    # Citations persisted exactly as generated.
    assert json.loads(ingredients) == RECIPE["ingredients"]
    assert instructions == RECIPE["instructions"]
    assert (prep_time, cuisine, meal_type) == (35, "Indian", "dinner")
    assert json.loads(nutrition) == RECIPE["nutrition_facts"]
    assert json.loads(compliance) == RECIPE["dietary_compliance"]
    assert health == 78.5
    assert generation_id == GENERATION["generation_id"]


def test_save_recipes_snapshot_identity_is_stored_as_json():
    snap = {"name": "seed27", "content_sha256": "ab" * 32, "as_of": "2026-10-16"}
    service = _service()
    service.save_recipes(7, [RECIPE], generation={**GENERATION, "snapshot": snap})
    gen_params = service.db.cursors[0].executed[0][1]
    assert json.loads(gen_params["snapshot"]) == snap


def test_save_recipes_without_generation_writes_no_generation_row():
    service = _service()
    bare = {k: RECIPE[k] for k in ("name", "ingredients", "instructions",
                                   "total_cost", "servings")}
    service.save_recipes(7, [bare])
    executed = service.db.cursors[0].executed
    assert len(executed) == 1
    params = executed[0][1]
    # Missing optional fields become NULL rather than raising.
    assert params[6:] == (None, None, None, None, None, None, None)


def test_rows_with_uuid_and_decimal_become_plain_json():
    from decimal import Decimal
    gid = uuid.uuid4()
    row = _jsonable({"generation_id": gid, "total_cost": Decimal("2.50"), "name": "x"})
    assert row == {"generation_id": str(gid), "total_cost": 2.5, "name": "x"}


# --------------------------------------------------------------------------
# finalize_meal_plan -> recipe_store
# --------------------------------------------------------------------------

class NullTracker:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _finalize_state(num_meals=2):
    recipe = {k: v for k, v in RECIPE.items()
              if k not in ("nutrition_facts", "health_score", "dietary_compliance")}
    return {
        "user_id": 7, "postal_code": "M5V3A8", "budget": 60.0,
        "budget_period": "weekly", "budget_spent_this_period": 10.0,
        "household_size": 2, "dietary_restrictions": ["vegetarian"],
        "num_meals": num_meals, "preferences": {},
        "available_deals": [{"deal_id": 17, "sale_price": 2.49, "regular_price": None}],
        "generated_recipes": {"r-1": recipe},
        "approved_recipe_ids": ["r-1"],
        "validation_results": {"r-1": {
            "recipe_id": "r-1", "approved": True, "feedback": "ok",
            "nutrition_facts": RECIPE["nutrition_facts"],
            "dietary_compliance": RECIPE["dietary_compliance"],
            "health_score": 78.5}},
        "iteration_count": 0,
        "generation_id": "gen-xyz",
        "provenance": {"models": {"chef": {"provider": "ollama", "model": "big"}},
                       "snapshot": None},
        "mlflow_run_id": "",
    }


def test_finalize_persists_provenance_and_nutrition_without_mutating_state():
    state = _finalize_state(num_meals=2)
    before = copy.deepcopy(state["generated_recipes"])
    store = InMemoryRecipeStore()

    update = graph.finalize_meal_plan(state, recipe_store=store, tracker=NullTracker())

    assert update["saved_recipe_ids"] == [1]
    assert state["generated_recipes"] == before  # enrichment is a copy

    gen = store.generations["gen-xyz"]
    assert gen["status"] == "partial"  # 1 of 2 requested
    assert gen["num_meals_requested"] == 2
    assert gen["num_recipes_saved"] == 1
    assert gen["dietary_restrictions"] == ["vegetarian"]
    assert (gen["budget"], gen["budget_period"], gen["budget_spent_this_period"]) == \
        (60.0, "weekly", 10.0)
    assert gen["models"] == {"chef": {"provider": "ollama", "model": "big"}}
    assert gen["snapshot"] is None
    assert gen["mlflow_run_id"] is None  # disabled MLflow's "" is not an id
    assert gen["total_cost"] == 2.49
    assert gen["estimated_savings"] is None

    (saved,) = store.generation_recipes("gen-xyz")
    assert saved["ingredients"] == RECIPE["ingredients"]
    assert saved["meal_type"] == "dinner"
    assert saved["cuisine_type"] == "Indian"
    assert saved["estimated_prep_time"] == 35
    assert saved["nutrition_facts"] == RECIPE["nutrition_facts"]
    assert saved["health_score"] == 78.5
    assert saved["dietary_compliance"] == RECIPE["dietary_compliance"]


def test_finalize_status_completed_when_every_requested_recipe_saved():
    store = InMemoryRecipeStore()
    graph.finalize_meal_plan(_finalize_state(num_meals=1), recipe_store=store,
                             tracker=NullTracker())
    assert store.generations["gen-xyz"]["status"] == "completed"


def test_in_memory_store_keeps_the_old_two_argument_call():
    store = InMemoryRecipeStore()
    assert store.save_recipes(1, [RECIPE, RECIPE]) == [1, 2]
    assert store.generations == {}
    assert [e["generation_id"] for e in store.saved] == [None, None]


# --------------------------------------------------------------------------
# The real graph, offline
# --------------------------------------------------------------------------

@pytest.fixture
def no_database_no_mlflow(monkeypatch):
    from app.db.database import Database
    from app.services import mlflow_logger

    def boom(*args, **kwargs):
        raise AssertionError("touched the database or MLflow")

    monkeypatch.setattr(Database, "initialize", boom)
    monkeypatch.setattr(Database, "get_connection", boom)
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", boom)


def test_one_generation_id_per_run_and_it_reaches_the_store(no_database_no_mlflow):
    deps = make_deps()
    first = _run(deps)
    second = _run(deps)

    assert first["status"] == second["status"] == "completed"
    assert first["generation_id"] != second["generation_id"]
    uuid.UUID(first["generation_id"])  # a real uuid

    store = deps.recipe_store
    assert set(store.generations) == {first["generation_id"], second["generation_id"]}
    for state in (first, second):
        gen = store.generations[state["generation_id"]]
        assert gen["dietary_restrictions"] == state["dietary_restrictions"]
        assert gen["budget_period"] == "week"
        assert gen["budget_spent_this_period"] == 10.0
        assert gen["models"] == configured_models()  # GraphDeps' default
        assert set(gen["models"]) == set(ROLES)
        assert gen["snapshot"] is None
        saved = store.generation_recipes(state["generation_id"])
        approved = _approved(state)
        assert [r["name"] for r in saved] == [r["name"] for r in approved]
        # Citations intact through persistence.
        assert [r["ingredients"] for r in saved] == [r["ingredients"] for r in approved]


def test_explicit_generation_id_and_models_are_honoured(no_database_no_mlflow):
    deps = make_deps()
    deps.models = {"chef": {"provider": "p", "model": "m"}}
    state = _run_with_id(deps, "11111111-2222-3333-4444-555555555555")
    assert state["generation_id"] == "11111111-2222-3333-4444-555555555555"
    gen = deps.recipe_store.generations[state["generation_id"]]
    assert gen["models"] == {"chef": {"provider": "p", "model": "m"}}


def _run_with_id(deps, generation_id):
    from app import main_recipe_generation
    from app.tests.test_graph_offline import FIXTURE
    user = FIXTURE["user"]
    return main_recipe_generation.run_recipe_generation(
        user_id=user["user_id"], postal_code=user["postal_code"], budget=user["budget"],
        household_size=user["household_size"],
        dietary_restrictions=user["dietary_restrictions"], num_meals=3,
        graph_deps=deps, generation_id=generation_id)


def test_eval_replay_records_snapshot_provenance_and_still_matches():
    """An eval run's state carries the snapshot identity and the recorded
    models; replaying an artifact recorded before provenance existed still
    reports no differences (generation_id / provenance are volatile)."""
    from app.eval import runner
    from app.tests.test_eval_runner import COMMITTED_ARTIFACT

    result = runner.replay(COMMITTED_ARTIFACT)
    assert result.ok, result.differences
    state = result.replayed["final_state"]
    assert state["provenance"]["snapshot"] == result.replayed["snapshot"]
    assert state["provenance"]["models"] == {
        role: {"provider": m["provider"], "model": m["model"]}
        for role, m in result.recorded["llm"].items()}
    uuid.UUID(state["generation_id"])
