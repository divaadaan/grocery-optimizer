"""Unit tests for the async recipe-generation job API.

Deterministic: the LangGraph workflow is replaced by a fake
`run_recipe_generation` that reports node events and returns a canned final
state, so nothing here needs Ollama or the database.
"""
import asyncio
import threading
import time

import httpx
import pytest
from fastapi import FastAPI

from app.routes import recipes as recipes_route
from app.services.recipe_jobs import (
    COMPLETED,
    FAILED,
    PENDING,
    PIPELINE_STAGES,
    RUNNING,
    RecipeJobRegistry,
    stage_after_node,
)

USER = {
    "user_id": 7,
    "postal_code": "M5V3A8",
    "budget": 75.0,
    "budget_period": "monthly",
    "household_size": 2,
    "dietary_restrictions": ["vegetarian"],
}

# What the stubbed budget read reports for USER: $20 of the $75 month gone.
BUDGET_STATUS = {
    "user_id": 7,
    "budget": 75.0,
    "budget_period": "monthly",
    "spent": 20.0,
    "remaining": 55.0,
    "available_for_plan": 55.0,
    "is_over_budget": False,
    "completed_lists": 1,
}

# A finished graph state with one approved, persisted recipe.
FINAL_STATE = {
    "status": "completed",
    "approved_recipe_ids": ["r1"],
    "saved_recipe_ids": [4242],
    "generated_recipes": {
        "r1": {
            "name": "Penne Pomodoro",
            "ingredients": [{"name": "Pasta Penne", "quantity": "1", "unit": "box", "price": 1.49}],
            "instructions": ["Boil pasta", "Add sauce"],
            "total_cost": 4.25,
            "servings": 2,
            "estimated_prep_time": 20,
            "meal_type": "dinner",
            "cuisine_type": "Italian",
        }
    },
    "validation_results": {"r1": {"nutrition_facts": {"calories": 520}, "health_score": 7.5}},
    "total_cost": 4.25,
    "cost_per_meal": 4.25,
    "estimated_savings": 1.5,
    "warnings": ["Only 1/2 recipes approved"],
}

# The nodes a healthy run emits, in order (3 parallel SousChef tasks).
HAPPY_PATH_NODES = [
    "initialize_chef",
    "plan_ingredient_groups",
    "sous_chef_generate",
    "sous_chef_generate",
    "sous_chef_generate",
    "aggregate_recipes",
    "nutritionist_validate",
    "finalize_meal_plan",
]


# --- stage mapping (pure) ---------------------------------------------------

def test_sous_chef_stage_counts_up_to_three():
    assert stage_after_node("sous_chef_generate", 1)[0] == "SousChef 2/3 cooking…"
    assert stage_after_node("sous_chef_generate", 2)[0] == "SousChef 3/3 cooking…"


def test_all_sous_chefs_done_moves_to_costing_stage():
    label, index = stage_after_node("sous_chef_generate", 3)
    assert label == PIPELINE_STAGES[3]
    assert index == 4


def test_unknown_node_leaves_stage_alone():
    assert stage_after_node("some_future_node", 0) is None


def test_stage_indexes_stay_within_the_pipeline():
    for node in HAPPY_PATH_NODES:
        stage = stage_after_node(node, 1)
        if stage is not None:
            assert 1 <= stage[1] <= len(PIPELINE_STAGES)


# --- registry ---------------------------------------------------------------

def test_new_job_is_pending_with_no_result():
    registry = RecipeJobRegistry()
    job = registry.create(user_id=7, num_meals=3)
    assert job.state == PENDING
    assert job.stage_index == 0
    assert job.result is None and job.error is None
    assert registry.get(job.job_id) is job


def test_unknown_job_id_is_none():
    assert RecipeJobRegistry().get("nope") is None


def test_stage_sequence_for_a_happy_path_run():
    registry = RecipeJobRegistry()
    job = registry.create(user_id=7, num_meals=3)
    registry.mark_running(job.job_id)

    stages = []
    for node in HAPPY_PATH_NODES:
        registry.report_node(job.job_id, node)
        stages.append(job.stage)

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


def test_report_node_advances_stage_only_while_not_terminal():
    registry = RecipeJobRegistry()
    job = registry.create(user_id=7, num_meals=3)
    registry.mark_running(job.job_id)
    assert job.state == RUNNING
    assert job.stage_index == 1

    registry.report_node(job.job_id, "plan_ingredient_groups")
    assert job.stage == "SousChef 1/3 cooking…"

    registry.complete(job.job_id, {"status": "completed"})
    registry.report_node(job.job_id, "initialize_chef")
    assert job.stage == "Meal plan ready"


def test_fail_after_complete_does_not_overwrite_result():
    registry = RecipeJobRegistry()
    job = registry.create(user_id=7, num_meals=3)
    registry.complete(job.job_id, {"status": "completed"})
    registry.fail(job.job_id, "too late")
    assert job.state == COMPLETED
    assert job.error is None


def test_terminal_jobs_are_evicted_past_the_cap_but_running_ones_are_kept():
    registry = RecipeJobRegistry(max_retained=2)
    live = registry.create(user_id=7, num_meals=1)
    registry.mark_running(live.job_id)
    old = [registry.create(user_id=7, num_meals=1) for _ in range(3)]
    for job in old:
        registry.complete(job.job_id, {"status": "completed"})
    # Creating one more trips eviction of the oldest finished jobs.
    registry.create(user_id=7, num_meals=1)
    assert registry.get(live.job_id) is not None
    assert registry.get(old[0].job_id) is None


def test_worker_exception_is_recorded_as_a_failed_job():
    registry = RecipeJobRegistry()
    job = registry.create(user_id=7, num_meals=1)

    def boom():
        raise RuntimeError("worker died")

    registry.submit(job.job_id, boom)
    _wait_terminal(registry, job.job_id)
    assert job.state == FAILED
    assert "worker died" in job.error


# --- graph streaming seam ---------------------------------------------------

class StubGraph:
    """Stands in for the compiled LangGraph; records how it was streamed."""

    def __init__(self, events):
        self._events = events
        self.stream_modes = []

    def stream(self, state, stream_mode=None):
        self.stream_modes.append(stream_mode)
        yield from self._events


# Minimal keys for run_recipe_generation's closing summary print.
STREAMED_FINAL_STATE = {
    "status": "completed",
    "generated_recipes": {},
    "approved_recipe_ids": [],
    "total_cost": 0.0,
    "iteration_count": 1,
}


def _run_streamed(monkeypatch, events, **kwargs):
    from app import main_recipe_generation

    stub = StubGraph(events)
    monkeypatch.setattr(main_recipe_generation, "create_recipe_generation_graph",
                        lambda deps=None: stub)
    result = main_recipe_generation.run_recipe_generation(
        user_id=1,
        postal_code="M5V3A8",
        budget=50.0,
        household_size=2,
        dietary_restrictions=[],
        num_meals=2,
        **kwargs,
    )
    return stub, result


def test_streamed_run_returns_the_last_values_chunk_and_reports_nodes(monkeypatch):
    """The "values" stream must stand in for invoke()'s return value."""
    events = [
        ("values", {"status": "initializing"}),
        ("updates", {"initialize_chef": {"status": "planning"}}),
        ("values", {"status": "planning"}),
        ("updates", {"sous_chef_generate": {}}),
        ("values", STREAMED_FINAL_STATE),
    ]
    seen = []
    stub, result = _run_streamed(monkeypatch, events, progress_callback=seen.append)

    assert result is STREAMED_FINAL_STATE
    assert seen == ["initialize_chef", "sous_chef_generate"]
    assert stub.stream_modes == [["updates", "values"]]


def test_streamed_run_works_without_a_progress_callback(monkeypatch):
    events = [("updates", {"initialize_chef": {}}), ("values", STREAMED_FINAL_STATE)]
    _, result = _run_streamed(monkeypatch, events)
    assert result is STREAMED_FINAL_STATE


def test_progress_callback_failure_does_not_fail_the_generation(monkeypatch):
    def angry(node):
        raise RuntimeError("progress sink exploded")

    events = [("updates", {"initialize_chef": {}}), ("values", STREAMED_FINAL_STATE)]
    _, result = _run_streamed(monkeypatch, events, progress_callback=angry)
    assert result is STREAMED_FINAL_STATE


def test_a_graph_that_yields_no_state_is_an_error(monkeypatch):
    with pytest.raises(RuntimeError):
        _run_streamed(monkeypatch, [("updates", {"initialize_chef": {}})])


# --- endpoints --------------------------------------------------------------

class AsgiClient:
    """
    Minimal sync HTTP client over an ASGI app.

    `starlette.testclient.TestClient` cannot be used in this environment:
    starlette 0.35.1 constructs `httpx.Client(app=...)`, and the installed
    httpx 0.28 removed that shortcut (TypeError on construction). Driving the
    app through `httpx.ASGITransport` is the same thing without the pin
    conflict. One event loop per request is fine — job state lives in the
    registry's thread pool, not on the loop.
    """

    def __init__(self, app):
        self._app = app

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def go():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self._app),
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(go())

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> httpx.Response:
        return self._request("POST", path, **kwargs)


@pytest.fixture
def client(monkeypatch):
    """A client over just the recipes router, with the DB and graph stubbed."""
    monkeypatch.setattr(
        recipes_route.UserService,
        "get_user_by_id",
        staticmethod(lambda user_id: USER if user_id == USER["user_id"] else None),
    )
    # The job worker reads how much of the budget period is already spent
    # (app/services/budget.py) before costing the plan; that is a DB read, so
    # it is stubbed here with the rest of the DB. BUDGET_STATUS says the user
    # is $20 into their $75 month.
    monkeypatch.setattr(recipes_route, "budget_status",
                        lambda user, **kw: dict(BUDGET_STATUS))
    # A fresh registry per test so job ids and eviction never leak across tests.
    monkeypatch.setattr(recipes_route, "job_registry", RecipeJobRegistry())

    app = FastAPI()
    app.include_router(recipes_route.router)
    return AsgiClient(app)


def _fake_generation(nodes=HAPPY_PATH_NODES, final_state=FINAL_STATE, raises=None):
    """Build a stand-in for run_recipe_generation that reports `nodes`."""
    seen = []

    def fake(progress_callback=None, **kwargs):
        for node in nodes:
            if progress_callback:
                progress_callback(node)
            seen.append(node)
        if raises is not None:
            raise raises
        return final_state

    fake.seen = seen
    return fake


def _install_fake_graph(monkeypatch, fake):
    from app import main_recipe_generation

    monkeypatch.setattr(main_recipe_generation, "run_recipe_generation", fake)


def _wait_terminal(registry, job_id, timeout=5.0):
    """Poll the registry until the job finishes (worker runs on a thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = registry.get(job_id)
        if job is not None and job.is_terminal:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def _poll_until_terminal(client, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/recipes/generate/status/{job_id}").json()
        if body["state"] in (COMPLETED, FAILED):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def test_post_generate_returns_a_job_handle_immediately(client, monkeypatch):
    _install_fake_graph(monkeypatch, _fake_generation())

    res = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2})
    assert res.status_code == 202
    body = res.json()
    assert body["user_id"] == 7
    assert body["state"] in (PENDING, RUNNING, COMPLETED)
    assert body["job_id"]
    assert body["status_url"].endswith(body["job_id"])
    assert "recipes" not in body


def test_post_generate_404s_for_unknown_user(client, monkeypatch):
    _install_fake_graph(monkeypatch, _fake_generation())
    res = client.post("/recipes/generate", json={"user_id": 999, "num_meals": 2})
    assert res.status_code == 404


def test_status_404s_for_unknown_job():
    app = FastAPI()
    app.include_router(recipes_route.router)
    res = AsgiClient(app).get("/recipes/generate/status/deadbeef")
    assert res.status_code == 404


def test_completed_job_carries_the_full_generation_response(client, monkeypatch):
    _install_fake_graph(monkeypatch, _fake_generation())

    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    body = _poll_until_terminal(client, job_id)

    assert body["state"] == COMPLETED
    assert body["error"] is None
    assert body["stage_total"] == len(PIPELINE_STAGES)
    assert body["elapsed_seconds"] >= 0

    result = body["result"]
    # The pre-job response contract, intact.
    assert set(result) == {
        "recipes", "total_cost", "cost_per_meal", "estimated_savings",
        "generation_time", "status", "warnings",
    }
    assert result["status"] == "completed"
    assert result["warnings"] == ["Only 1/2 recipes approved"]
    assert float(result["total_cost"]) == 4.25
    assert len(result["recipes"]) == 1

    recipe = result["recipes"][0]
    # The persisted DB id from saved_recipe_ids must reach the client.
    assert recipe["recipe_id"] == 4242
    assert recipe["name"] == "Penne Pomodoro"
    assert recipe["nutrition_facts"] == {"calories": 520}
    assert float(recipe["health_score"]) == 7.5


def test_status_reports_the_live_stage_while_running(client, monkeypatch):
    """The graph's progress_callback must reach the status endpoint."""
    reached_sous_chefs = threading.Event()
    release = threading.Event()

    def fake(progress_callback=None, **kwargs):
        progress_callback("initialize_chef")
        progress_callback("plan_ingredient_groups")
        reached_sous_chefs.set()
        release.wait(5)
        return FINAL_STATE

    _install_fake_graph(monkeypatch, fake)
    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    assert reached_sous_chefs.wait(5)

    body = client.get(f"/recipes/generate/status/{job_id}").json()
    assert body["state"] == RUNNING
    assert body["stage"] == "SousChef 1/3 cooking…"
    assert body["stage_index"] == 3
    assert body["pipeline"] == list(PIPELINE_STAGES)
    assert body["result"] is None and body["error"] is None

    release.set()
    assert _poll_until_terminal(client, job_id)["state"] == COMPLETED


def test_generation_is_costed_against_the_unspent_part_of_the_period(client, monkeypatch):
    """The worker must forward period spend, not just the period budget.

    Without this the plan is priced against the whole week's or month's money
    however much of it the user has already spent — the bug this wiring fixes.
    """
    captured = {}

    def fake(progress_callback=None, **kwargs):
        captured.update(kwargs)
        return FINAL_STATE

    _install_fake_graph(monkeypatch, fake)
    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    assert _poll_until_terminal(client, job_id)["state"] == COMPLETED

    assert captured["budget"] == 75.0
    assert captured["budget_period"] == "monthly"
    assert captured["budget_spent_this_period"] == 20.0


def test_a_failed_budget_read_does_not_sink_the_generation(client, monkeypatch):
    """Degrade to the full period budget rather than failing the job."""
    captured = {}

    def boom(user, **kw):
        raise RuntimeError("neon is unreachable")

    monkeypatch.setattr(recipes_route, "budget_status", boom)

    def fake(progress_callback=None, **kwargs):
        captured.update(kwargs)
        return FINAL_STATE

    _install_fake_graph(monkeypatch, fake)
    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    assert _poll_until_terminal(client, job_id)["state"] == COMPLETED
    assert captured["budget_spent_this_period"] == 0.0


def test_workflow_exception_becomes_a_failed_job(client, monkeypatch):
    _install_fake_graph(monkeypatch, _fake_generation(raises=RuntimeError("ollama is down")))

    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    body = _poll_until_terminal(client, job_id)

    assert body["state"] == FAILED
    assert "ollama is down" in body["error"]
    assert body["result"] is None


def test_unsuccessful_graph_status_becomes_a_failed_job(client, monkeypatch):
    failed_state = {"status": "failed", "errors": ["Unable to generate sufficient approved recipes"]}
    _install_fake_graph(monkeypatch, _fake_generation(nodes=["handle_failure"], final_state=failed_state))

    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    body = _poll_until_terminal(client, job_id)

    assert body["state"] == FAILED
    assert "Unable to generate sufficient approved recipes" in body["error"]
    assert body["result"] is None


def test_concurrent_jobs_are_tracked_independently(client, monkeypatch):
    _install_fake_graph(monkeypatch, _fake_generation())

    ids = [
        client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
        for _ in range(3)
    ]
    assert len(set(ids)) == 3
    for job_id in ids:
        assert _poll_until_terminal(client, job_id)["state"] == COMPLETED
