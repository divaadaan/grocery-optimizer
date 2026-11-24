"""Unit tests for the async recipe-generation job API.

Deterministic: the generation pipeline is replaced by a fake
`run_plan_generation` that reports stage events and returns a canned result,
so nothing here needs Ollama or the database.
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

# A finished graph state with one approved, persisted recipe.
FINAL_STATE = {
    "status": "completed",
    "generation_id": "6f1c2e5b-7a4d-4f0e-8b3a-6c1d2e5f7a90",
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

# The stages a healthy run reports, in order (plan_pipeline.progress).
HAPPY_PATH_NODES = [
    "confirm_selection",
    "plan_dishes",
    "write_recipes",
    "dietary_verdict",
    "finalize_meal_plan",
]


# --- stage mapping (pure) ---------------------------------------------------

def test_each_stage_maps_to_what_is_now_running():
    assert stage_after_node("confirm_selection") == (PIPELINE_STAGES[1], 2)
    assert stage_after_node("plan_dishes") == (PIPELINE_STAGES[2], 3)
    assert stage_after_node("write_recipes") == (PIPELINE_STAGES[4], 5)
    assert stage_after_node("dietary_verdict") == (PIPELINE_STAGES[5], 6)


def test_unknown_node_leaves_stage_alone():
    assert stage_after_node("some_future_node") is None


def test_stage_indexes_stay_within_the_pipeline():
    for node in HAPPY_PATH_NODES:
        stage = stage_after_node(node)
        if stage is not None:
            assert 1 <= stage[1] <= len(PIPELINE_STAGES)


def test_stages_match_the_pipelines_progress_calls():
    import inspect
    from app.agents import plan_pipeline
    src = inspect.getsource(plan_pipeline)
    for node in HAPPY_PATH_NODES + ["handle_failure"]:
        assert f'progress("{node}")' in src, node


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
        PIPELINE_STAGES[2],
        PIPELINE_STAGES[4],
        PIPELINE_STAGES[5],
        PIPELINE_STAGES[5],
    ]


def test_report_node_advances_stage_only_while_not_terminal():
    registry = RecipeJobRegistry()
    job = registry.create(user_id=7, num_meals=3)
    registry.mark_running(job.job_id)
    assert job.state == RUNNING
    assert job.stage_index == 1

    registry.report_node(job.job_id, "plan_dishes")
    assert job.stage == PIPELINE_STAGES[2]

    registry.complete(job.job_id, {"status": "completed"})
    registry.report_node(job.job_id, "confirm_selection")
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
    # A fresh registry per test so job ids and eviction never leak across tests.
    monkeypatch.setattr(recipes_route, "job_registry", RecipeJobRegistry())

    app = FastAPI()
    app.include_router(recipes_route.router)
    return AsgiClient(app)


def _fake_generation(nodes=HAPPY_PATH_NODES, final_state=FINAL_STATE, raises=None):
    """Build a stand-in for run_plan_generation that reports `nodes`."""
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

    monkeypatch.setattr(main_recipe_generation, "run_plan_generation", fake)


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
    # The pre-job response contract, intact, plus Track C's additive fields.
    assert set(result) == {
        "recipes", "total_cost", "cost_per_meal", "estimated_savings",
        "generation_time", "status", "warnings", "generation_id",
        "meals_requested", "partial", "budget_check",
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
    # The generation id reaches the client on the plan and on each recipe.
    assert result["generation_id"] == FINAL_STATE["generation_id"]
    assert recipe["generation_id"] == FINAL_STATE["generation_id"]


def test_status_reports_the_live_stage_while_running(client, monkeypatch):
    """The graph's progress_callback must reach the status endpoint."""
    reached_sous_chefs = threading.Event()
    release = threading.Event()

    def fake(progress_callback=None, **kwargs):
        progress_callback("confirm_selection")
        progress_callback("plan_dishes")
        reached_sous_chefs.set()
        release.wait(5)
        return FINAL_STATE

    _install_fake_graph(monkeypatch, fake)
    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    assert reached_sous_chefs.wait(5)

    body = client.get(f"/recipes/generate/status/{job_id}").json()
    assert body["state"] == RUNNING
    assert body["stage"] == PIPELINE_STAGES[2]
    assert body["stage_index"] == 3
    assert body["pipeline"] == list(PIPELINE_STAGES)
    assert body["result"] is None and body["error"] is None

    release.set()
    assert _poll_until_terminal(client, job_id)["state"] == COMPLETED


def test_worker_forwards_the_confirmed_selection_and_no_budget(client, monkeypatch):
    """Approach C: the kept deals and confirmed pantry reach the pipeline;
    the product is budget-agnostic, so no budget is forwarded."""
    captured = {}

    def fake(progress_callback=None, **kwargs):
        captured.update(kwargs)
        return FINAL_STATE

    _install_fake_graph(monkeypatch, fake)
    job_id = client.post("/recipes/generate", json={
        "user_id": 7, "num_meals": 2, "kept_deal_ids": [3, 1],
        "pantry_product_ids": [9]}).json()["job_id"]
    assert _poll_until_terminal(client, job_id)["state"] == COMPLETED
    assert captured["kept_deal_ids"] == [3, 1]
    assert captured["pantry_product_ids"] == [9]
    assert "budget" not in captured


def test_omitted_selection_means_keep_everything_offered(client, monkeypatch):
    captured = {}

    def fake(progress_callback=None, **kwargs):
        captured.update(kwargs)
        return FINAL_STATE

    _install_fake_graph(monkeypatch, fake)
    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    assert _poll_until_terminal(client, job_id)["state"] == COMPLETED
    assert captured["kept_deal_ids"] is None and captured["pantry_product_ids"] is None


def test_a_selection_without_anchors_fails_the_job_with_the_reason(client, monkeypatch):
    from app.agents.plan_pipeline import SelectionError

    def fake(progress_callback=None, **kwargs):
        raise SelectionError("Keep at least one anchor (protein) to plan dishes around.")

    _install_fake_graph(monkeypatch, fake)
    job_id = client.post("/recipes/generate", json={"user_id": 7, "num_meals": 2}).json()["job_id"]
    body = _poll_until_terminal(client, job_id)
    assert body["state"] == FAILED and "anchor" in body["error"]


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
