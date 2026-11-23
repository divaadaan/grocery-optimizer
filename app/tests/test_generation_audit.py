"""A generation run is auditable from the database (migration 023).

* a failed run writes a ``status='failed'`` ``recipe_generations`` row with
  the same provenance a finalized one has, a failure reason, run counts and
  no recipes -- and a failure to write it never masks the failure itself;
* recipes persist their ``instruction_steps`` and their Nutritionist verdict
  in a dedicated ``dietary_compliance`` column, and the readers return both;
* a failed job exposes its ``generation_id``.

No database, no LLM: ``DatabaseService`` runs against the fake cursor of
``test_recipe_persistence``, and the real graph runs offline on scripted
models (``test_orchestration_core`` / ``test_graph_offline``).
"""
import re
import uuid
from pathlib import Path

import pytest

from app.agents import graph
from app.agents.deps import InMemoryRecipeStore
from app.routes import recipes as recipes_route
from app.services.database import DatabaseService
from app.services.recipe_jobs import FAILED, COMPLETED
from app.tests.test_graph_offline import ScriptedChatModel, no_database_no_mlflow  # noqa: F401
from app.tests.test_orchestration_core import THREE_GROUPS, make_deps, run, sous_reply
from app.tests.test_recipe_jobs import (  # noqa: F401  (client is a fixture)
    FINAL_STATE, _fake_generation, _install_fake_graph, _poll_until_terminal, client)
from app.tests.test_recipe_persistence import (
    GENERATION, RECIPE, NullTracker, _finalize_state, _service)

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "023_generation_audit.sql"


# --------------------------------------------------------------------------
# handle_failure -> recipe_store
# --------------------------------------------------------------------------

def _failed_state(**overrides):
    state = {
        **_finalize_state(num_meals=3),
        "status": "validating",
        "ingredient_groups": [[{"deal_id": 17}]],
        "approved_recipe_ids": ["r-1"],
        "rejected_recipe_ids": ["r-2", "r-3"],
        "iteration_count": 2,
        "max_iterations": 2,
        "total_cost": 7.5,
        "cost_per_meal": 2.5,
        "estimated_savings": None,
        "mlflow_run_id": "run-42",
        "errors": [],
    }
    state["generated_recipes"] = {**state["generated_recipes"],
                                  "r-2": RECIPE, "r-3": RECIPE}
    state.update(overrides)
    return state


def test_handle_failure_records_a_failed_generation_with_provenance():
    store = InMemoryRecipeStore()
    update = graph.handle_failure(_failed_state(), recipe_store=store,
                                  tracker=NullTracker())

    assert update["status"] == "failed"
    assert update["errors"] == ["Unable to generate sufficient approved recipes"]
    assert "warnings" not in update
    assert store.saved == []  # no recipes

    gen = store.generations["gen-xyz"]
    assert gen["status"] == "failed"
    assert gen["num_recipes_saved"] == 0
    assert gen["num_meals_requested"] == 3
    # The same provenance finalize writes.
    assert gen["dietary_restrictions"] == ["vegetarian"]
    assert (gen["budget"], gen["budget_period"], gen["budget_spent_this_period"]) == \
        (60.0, "weekly", 10.0)
    assert (gen["household_size"], gen["postal_code"]) == (2, "M5V3A8")
    assert gen["models"] == {"chef": {"provider": "ollama", "model": "big"}}
    assert gen["snapshot"] is None
    assert gen["mlflow_run_id"] == "run-42"
    # There is no plan, so no plan totals -- not the interim aggregate figure.
    assert gen["total_cost"] is None and gen["estimated_savings"] is None
    # Run counts and the reason.
    assert (gen["iteration_count"], gen["num_recipes_generated"],
            gen["num_recipes_approved"], gen["num_recipes_rejected"]) == (2, 3, 1, 2)
    assert gen["failure_reason"] == (
        "Insufficient approved recipes: 1/3 approved after 2 retry iteration(s) "
        "(max 2), 2 still rejected")


def test_planning_failure_reason_carries_the_chef_error():
    state = _failed_state(status="failed", ingredient_groups=[],
                          errors=["Chef planning failed: boom"],
                          approved_recipe_ids=[], generated_recipes={})
    assert graph.failure_reason(state) == "Planning failed: Chef planning failed: boom"


class BrokenStore:
    def save_recipes(self, *args, **kwargs):
        raise ConnectionError("SSL connection has been closed unexpectedly")


def test_a_failed_audit_write_does_not_mask_the_failure():
    update = graph.handle_failure(_failed_state(), recipe_store=BrokenStore(),
                                  tracker=NullTracker())
    assert update["status"] == "failed"
    assert update["errors"] == ["Unable to generate sufficient approved recipes"]
    assert update["warnings"] == [
        "The failed generation could not be recorded (ConnectionError)"]


def test_a_failed_save_in_the_success_path_still_raises():
    with pytest.raises(ConnectionError):
        graph.finalize_meal_plan(_finalize_state(num_meals=1),
                                 recipe_store=BrokenStore(), tracker=NullTracker())


def test_finalized_generations_record_run_counts_and_no_failure_reason():
    store = InMemoryRecipeStore()
    graph.finalize_meal_plan({**_finalize_state(num_meals=1), "rejected_recipe_ids": []},
                             recipe_store=store, tracker=NullTracker())
    gen = store.generations["gen-xyz"]
    assert gen["status"] == "completed"
    assert gen["failure_reason"] is None
    assert (gen["iteration_count"], gen["num_recipes_generated"],
            gen["num_recipes_approved"], gen["num_recipes_rejected"]) == (0, 1, 1, 0)


# --------------------------------------------------------------------------
# The real graph, offline: both routes into handle_failure persist a row
# --------------------------------------------------------------------------

def test_real_graph_failed_validation_run_writes_a_failed_row(no_database_no_mlflow):  # noqa: F811
    # Every SousChef call returns nothing: retries run out with 0 approved.
    deps = make_deps(THREE_GROUPS, sous=sous_reply(short=99))
    state = run(deps, num_meals=3)

    assert state["status"] == "failed"
    uuid.UUID(state["generation_id"])
    store = deps.recipe_store
    assert store.saved == []
    gen = store.generations[state["generation_id"]]
    assert gen["status"] == "failed"
    assert gen["num_recipes_saved"] == 0
    assert gen["num_recipes_generated"] == 0
    assert gen["iteration_count"] == state["max_iterations"]
    assert gen["failure_reason"].startswith("Insufficient approved recipes: 0/3")
    assert gen["models"] == deps.models


def test_real_graph_planning_failure_writes_a_failed_row(no_database_no_mlflow):  # noqa: F811
    deps = make_deps(THREE_GROUPS)
    deps.chef_llm = ScriptedChatModel(respond=lambda p: "not json at all", prompts=[])
    state = run(deps, num_meals=3)

    assert state["status"] == "failed"
    gen = deps.recipe_store.generations[state["generation_id"]]
    assert gen["status"] == "failed"
    assert gen["failure_reason"].startswith("Planning failed: Chef planning failed")
    assert gen["num_recipes_generated"] == 0


# --------------------------------------------------------------------------
# DatabaseService against a fake cursor
# --------------------------------------------------------------------------

FAILED_GENERATION = {**GENERATION, "status": "failed", "num_recipes_saved": 0,
                     "total_cost": None, "failure_reason": "Planning failed: x",
                     "iteration_count": 0, "num_recipes_generated": 0,
                     "num_recipes_approved": 0, "num_recipes_rejected": 0}


def test_save_recipes_writes_a_failed_generation_row_and_no_recipes():
    service = _service()
    assert service.save_recipes(7, [], generation=FAILED_GENERATION) == []
    ((sql, params),) = service.db.cursors[0].executed
    assert sql.startswith("INSERT INTO recipe_generations")
    assert params["status"] == "failed"
    assert params["num_recipes_saved"] == 0
    assert params["failure_reason"] == "Planning failed: x"
    assert params["total_cost"] is None
    for column in ("failure_reason", "iteration_count", "num_recipes_generated",
                   "num_recipes_approved", "num_recipes_rejected"):
        assert column in sql


def test_save_recipes_counts_are_null_when_the_caller_has_none():
    service = _service()
    service.save_recipes(7, [RECIPE], generation=GENERATION)
    params = service.db.cursors[0].executed[0][1]
    assert params["failure_reason"] is None
    assert params["iteration_count"] is None


class RowCursor:
    def __init__(self, rows):
        self.rows, self.executed = rows, []

    def execute(self, query, params=None):
        self.executed.append(" ".join(query.split()))

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


ROW = {"recipe_id": 5, "user_id": 7, "name": "Lentil Curry",
       "ingredients": RECIPE["ingredients"], "instructions": RECIPE["instructions"],
       "instruction_steps": RECIPE["instruction_steps"], "total_cost": 2.49,
       "servings": 2, "prep_time": 35, "cook_time": None, "cuisine_type": "Indian",
       "meal_type": "dinner", "nutritional_info": {"calories": 480},
       "dietary_compliance": RECIPE["dietary_compliance"], "health_score": 78.5,
       "generation_id": uuid.UUID(GENERATION["generation_id"]),
       "created_at": "2026-11-23T12:00:00"}


@pytest.mark.parametrize("read", [
    lambda s: s.get_recipe(5),
    lambda s: s.get_user_recipes(7),
    lambda s: s.get_generation_recipes(7, GENERATION["generation_id"]),
])
def test_recipe_readers_select_the_new_columns_and_not_allergen_info(read):
    import contextlib
    service = DatabaseService.__new__(DatabaseService)
    cursor = RowCursor([ROW])

    class Db:
        @contextlib.contextmanager
        def get_cursor(self):
            yield cursor

    service.db = Db()
    out = read(service)
    row = out if isinstance(out, dict) else out[0]
    sql = cursor.executed[0]
    assert "instruction_steps" in sql and "dietary_compliance" in sql
    assert "allergen_info" not in sql  # dietary_compliance is the one source
    assert row["instruction_steps"] == RECIPE["instruction_steps"]
    assert row["dietary_compliance"] == RECIPE["dietary_compliance"]


def test_latest_generation_skips_failed_runs():
    import contextlib
    service = DatabaseService.__new__(DatabaseService)
    cursor = RowCursor([])

    class Db:
        @contextlib.contextmanager
        def get_cursor(self):
            yield cursor

    service.db = Db()
    assert service.get_latest_generation_id(7) is None
    assert "status <> 'failed'" in cursor.executed[0]


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def test_get_recipe_returns_instruction_steps_and_compliance(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(recipes_route.db_service, "get_recipe",
                        lambda rid: {**ROW, "generation_id": GENERATION["generation_id"]})
    body = client.get("/recipes/5").json()
    assert body["instruction_steps"] == RECIPE["instruction_steps"]
    assert body["dietary_compliance"] == RECIPE["dietary_compliance"]


def test_get_user_recipes_returns_instruction_steps_and_compliance(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(recipes_route.db_service, "get_user_recipes",
                        lambda uid, limit, generation_id=None: [
                            {**ROW, "generation_id": GENERATION["generation_id"]}])
    (body,) = client.get("/recipes/user/7").json()
    assert body["instruction_steps"] == RECIPE["instruction_steps"]
    assert body["dietary_compliance"] == RECIPE["dietary_compliance"]


def test_legacy_rows_without_the_new_columns_read_as_null(client, monkeypatch):  # noqa: F811
    legacy = {k: v for k, v in ROW.items()
              if k not in ("instruction_steps", "dietary_compliance")}
    monkeypatch.setattr(recipes_route.db_service, "get_recipe",
                        lambda rid: {**legacy, "generation_id": None})
    body = client.get("/recipes/5").json()
    assert body["instruction_steps"] is None and body["dietary_compliance"] is None


def test_failed_job_exposes_its_generation_id(client, monkeypatch):  # noqa: F811
    gid = "11111111-2222-3333-4444-555555555555"
    failed_state = {"status": "failed", "generation_id": gid,
                    "errors": ["Unable to generate sufficient approved recipes"]}
    _install_fake_graph(monkeypatch, _fake_generation(nodes=["handle_failure"],
                                                      final_state=failed_state))
    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    body = _poll_until_terminal(client, job_id)
    assert body["state"] == FAILED
    assert body["generation_id"] == gid


def test_completed_job_status_carries_the_generation_id_too(client, monkeypatch):  # noqa: F811
    _install_fake_graph(monkeypatch, _fake_generation())
    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    body = _poll_until_terminal(client, job_id)
    assert body["state"] == COMPLETED
    assert body["generation_id"] == FINAL_STATE["generation_id"]
    assert body["result"]["generation_id"] == FINAL_STATE["generation_id"]


# --------------------------------------------------------------------------
# Migration 023
# --------------------------------------------------------------------------

def _sql():
    # Comments stripped: the rollback block repeats the old CHECK.
    return "\n".join(line.split("--", 1)[0] for line in MIGRATION.read_text().splitlines())


def test_migration_023_widens_the_status_check_to_failed():
    sql = _sql()
    assert "DROP CONSTRAINT IF EXISTS chk_recipe_generations_status" in sql
    assert re.search(r"CHECK \(status IN \('completed', 'partial', 'failed'\)\)", sql)


def test_migration_023_ties_failure_reason_to_failed_status():
    sql = _sql()
    assert "(status = 'failed') = (failure_reason IS NOT NULL)" in sql
    assert "status <> 'failed' OR num_recipes_saved = 0" in sql


def test_migration_023_columns_match_what_the_code_writes():
    sql = _sql()
    for table, column in [
        ("recipe_generations", "failure_reason"),
        ("recipe_generations", "iteration_count"),
        ("recipe_generations", "num_recipes_generated"),
        ("recipe_generations", "num_recipes_approved"),
        ("recipe_generations", "num_recipes_rejected"),
        ("recipes", "instruction_steps"),
        ("recipes", "dietary_compliance"),
    ]:
        assert re.search(rf"ALTER TABLE {table}\s+ADD COLUMN IF NOT EXISTS {column}\b",
                         sql), (table, column)


def test_migration_023_backfill_is_guarded_and_idempotent():
    sql = _sql()
    backfill = sql[sql.index("UPDATE recipes"):]
    backfill = backfill[:backfill.index(";")]
    assert "SET dietary_compliance = allergen_info" in backfill
    assert "dietary_compliance IS NULL" in backfill        # re-run is a no-op
    assert "allergen_info ? 'meets_restrictions'" in backfill  # only verdicts
    # Additive first: the legacy column is neither dropped nor cleared.
    assert "DROP COLUMN" not in sql
    assert "SET allergen_info" not in sql
