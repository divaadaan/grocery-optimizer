"""A generation run is auditable from the database (migration 023).

* a failed run writes a ``status='failed'`` ``recipe_generations`` row with
  the same provenance a finalized one has, a failure reason, run counts and
  no recipes (the pipeline side -- a failed audit write never masks the
  failure -- is in ``test_plan_pipeline``);
* recipes persist their ``instruction_steps`` and their dietary verdict in a
  dedicated ``dietary_compliance`` column, and the readers return both;
* a failed job exposes its ``generation_id``.

No database, no LLM: ``DatabaseService`` runs against the fake cursor of
``test_recipe_persistence``.
"""
import re
import uuid
from pathlib import Path

import pytest

from app.routes import recipes as recipes_route
from app.services.database import DatabaseService
from app.services.recipe_jobs import FAILED, COMPLETED
from app.tests.test_recipe_jobs import (  # noqa: F401  (client is a fixture)
    FINAL_STATE, _fake_generation, _install_fake_graph, _poll_until_terminal, client)
from app.tests.test_recipe_persistence import (
    GENERATION, RECIPE, _service)

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "023_generation_audit.sql"


# --------------------------------------------------------------------------
# handle_failure -> recipe_store
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
