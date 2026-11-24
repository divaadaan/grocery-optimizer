"""Recipe persistence: one generation, its provenance, and everything a
recipe carries (Track C3, migration 022).

No database: ``DatabaseService`` runs against a fake cursor. The pipeline
side (one generation id per run, provenance reaching the store) is covered in
``test_plan_pipeline``.
"""
import contextlib
import copy
import json
import uuid

import pytest

from app.agents.plan_pipeline import InMemoryRecipeStore
from app.services.database import DatabaseService, _jsonable


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
    "instruction_steps": [{"template": "Simmer {0}", "ingredient_refs": [0]},
                          {"template": "Season with {1}", "ingredient_refs": [1]}],
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
    (user_id, name, ingredients, instructions, steps, total_cost, servings,
     prep_time, cuisine, meal_type, nutrition, compliance, health,
     generation_id) = recipe_params
    assert (user_id, name, servings, total_cost) == (7, "Lentil Curry", 2, 2.49)
    # Citations persisted exactly as generated.
    assert json.loads(ingredients) == RECIPE["ingredients"]
    assert instructions == RECIPE["instructions"]
    # The placeholder steps behind the rendered text (migration 023).
    assert json.loads(steps) == RECIPE["instruction_steps"]
    assert (prep_time, cuisine, meal_type) == (35, "Indian", "dinner")
    assert json.loads(nutrition) == RECIPE["nutrition_facts"]
    assert json.loads(compliance) == RECIPE["dietary_compliance"]
    assert health == 78.5
    assert generation_id == GENERATION["generation_id"]
    # The verdict has its own column; the legacy one is no longer written.
    assert "dietary_compliance" in recipe_sql and "allergen_info" not in recipe_sql
    assert "instruction_steps" in recipe_sql


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
    assert params[4] is None  # instruction_steps
    assert params[7:] == (None, None, None, None, None, None, None)


def test_rows_with_uuid_and_decimal_become_plain_json():
    from decimal import Decimal
    gid = uuid.uuid4()
    row = _jsonable({"generation_id": gid, "total_cost": Decimal("2.50"), "name": "x"})
    assert row == {"generation_id": str(gid), "total_cost": 2.5, "name": "x"}


# --------------------------------------------------------------------------
# finalize_meal_plan -> recipe_store
# --------------------------------------------------------------------------

def test_in_memory_store_keeps_the_old_two_argument_call():
    store = InMemoryRecipeStore()
    assert store.save_recipes(1, [RECIPE, RECIPE]) == [1, 2]
    assert store.generations == {}
    assert [e["generation_id"] for e in store.saved] == [None, None]
