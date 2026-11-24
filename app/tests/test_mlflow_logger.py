"""MLflow logging: one run per generation across threads, honest tokens.

A fake ``mlflow`` module records what the logger does. Nothing reaches a
tracking server. The pipeline-level test drives the real generation pipeline
(its per-dish SousChefs execute on pool threads) with the real
``MLflowLogger`` as tracker.
"""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace

import pytest
from app.services import mlflow_logger
from app.services.mlflow_logger import MLflowLogger, generation_scope


class FakeClient:
    def __init__(self, backend):
        self.b = backend

    def create_run(self, experiment_id, run_name=None, tags=None):
        with self.b.lock:
            run_id = f"run{len(self.b.runs) + 1}"
            self.b.runs[run_id] = {"name": run_name, "tags": dict(tags or {}), "params": {},
                                   "metrics": {}, "artifacts": {}, "status": "RUNNING",
                                   "experiment_id": experiment_id}
        return SimpleNamespace(info=SimpleNamespace(run_id=run_id))

    def log_param(self, run_id, key, value):
        params = self.b.runs[run_id]["params"]
        if key in params and params[key] != value:
            raise RuntimeError(f"param {key} changed")  # MLflow refuses this too
        params[key] = value

    def log_metric(self, run_id, key, value, timestamp=None, step=None):
        with self.b.lock:
            self.b.runs[run_id]["metrics"].setdefault(key, []).append(value)

    def set_tag(self, run_id, key, value):
        self.b.runs[run_id]["tags"][key] = value

    def log_dict(self, run_id, dictionary, artifact_file):
        self.b.runs[run_id]["artifacts"][artifact_file] = dictionary

    def log_text(self, run_id, text, artifact_file):
        self.b.runs[run_id]["artifacts"][artifact_file] = text

    def set_terminated(self, run_id, status=None):
        self.b.runs[run_id]["status"] = status


class FakeMlflow:
    """Only the module-level API the logger may use. The fluent run API is
    deliberately absent: using it is how pool threads made orphan runs."""

    def __init__(self):
        self.runs = {}
        self.lock = threading.Lock()

    def set_tracking_uri(self, uri):
        pass

    def set_experiment(self, name):
        return SimpleNamespace(experiment_id="exp1")

    def MlflowClient(self):
        return FakeClient(self)


@pytest.fixture
def fake_mlflow(monkeypatch):
    fake = FakeMlflow()
    monkeypatch.setattr(mlflow_logger, "mlflow", fake)
    monkeypatch.setattr(mlflow_logger, "_configured", False)
    monkeypatch.setattr(mlflow_logger, "_available", False)
    monkeypatch.setattr(mlflow_logger, "_client", None)
    monkeypatch.setattr(mlflow_logger, "_experiment_id", None)
    # No tracking-server probe, no process-wide env writes: the fake is the server.
    monkeypatch.setattr(mlflow_logger, "_probe", lambda uri, timeout=None: None)
    monkeypatch.setattr(mlflow_logger, "_cap_http_retries", lambda: None)
    return fake


MODELS = {"chef": {"provider": "ollama", "model": "big"},
          "sous_chef": {"provider": "ollama", "model": "mid"}}


def _start(gid="gen-1"):
    return MLflowLogger.start_run(user_id=1, num_meals=3, budget=50.0,
                                  dietary_restrictions=["vegan"], generation_id=gid,
                                  models=MODELS)


def test_pool_thread_calls_land_in_the_generation_run(fake_mlflow):
    with generation_scope("gen-1"):
        run_id = _start()

        def sous_chef(i):
            MLflowLogger.log_agent_call(agent_name=f"sous_chef_{i}", duration=1.0,
                                        model="mid", success=True,
                                        usage={"input_tokens": 10, "output_tokens": 5,
                                               "total_tokens": 15})

        # LangGraph runs each task in a copy of the caller's context; so do we.
        with ThreadPoolExecutor(max_workers=3) as pool:
            for f in [pool.submit(copy_context().run, sous_chef, i) for i in (1, 2, 3)]:
                f.result()
        MLflowLogger.finalize_run({"status": "completed"})

    assert list(fake_mlflow.runs) == [run_id]  # no orphan runs
    run = fake_mlflow.runs[run_id]
    for i in (1, 2, 3):
        assert run["metrics"][f"sous_chef_{i}_total_tokens"] == [15]
        assert run["tags"][f"sous_chef_{i}_model"] == "mid"
    assert run["tags"]["generation_id"] == "gen-1"
    assert run["params"]["generation_id"] == "gen-1"
    assert run["params"]["model_chef"] == "big"
    assert run["params"]["provider_sous_chef"] == "ollama"
    assert "model_nutritionist" not in run["params"]
    assert run["status"] == "FINISHED"


def test_concurrent_generations_do_not_share_a_run(fake_mlflow):
    barrier = threading.Barrier(2)
    seen = {}

    def generation(gid):
        with generation_scope(gid):
            run_id = _start(gid)
            barrier.wait()  # both runs exist before either logs
            MLflowLogger.log_agent_call(agent_name="chef", duration=0.1, model="big",
                                        success=True)
            seen[gid] = run_id

    threads = [threading.Thread(target=generation, args=(g,)) for g in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen["a"] != seen["b"]
    for gid, run_id in seen.items():
        run = fake_mlflow.runs[run_id]
        assert run["tags"]["generation_id"] == gid
        assert run["metrics"]["chef_duration_sec"] == [0.1]


def test_no_run_means_nothing_is_logged_and_no_run_is_created(fake_mlflow):
    MLflowLogger.log_agent_call(agent_name="sous_chef_1", duration=1.0, model="m",
                                success=True)
    MLflowLogger.log_final_metrics(total_cost=1, cost_per_meal=1, estimated_savings=None,
                                   iterations=0, recipe_count=1, success=True)
    MLflowLogger.finalize_run({})
    assert fake_mlflow.runs == {}


def test_tokens_only_from_usage_metadata(fake_mlflow):
    with generation_scope("g"):
        run_id = _start("g")
        MLflowLogger.log_agent_call(agent_name="chef", duration=1.0, model="big",
                                    success=True, usage=None)
        MLflowLogger.log_agent_call(agent_name="sous_chef_1", duration=1.0, model="mid",
                                    success=True,
                                    usage={"input_tokens": 7, "output_tokens": None,
                                           "total_tokens": True})
    metrics = fake_mlflow.runs[run_id]["metrics"]
    assert not any(k.startswith("chef_") and "token" in k for k in metrics)
    assert metrics["sous_chef_1_input_tokens"] == [7]
    assert "sous_chef_1_output_tokens" not in metrics
    assert "sous_chef_1_total_tokens" not in metrics  # a bool is not a count
    assert not any(k.endswith("_tokens") and k.count("_") == 1 for k in metrics)


def test_failed_generation_run_is_marked_failed(fake_mlflow):
    with generation_scope("g"):
        run_id = _start("g")
        MLflowLogger.finalize_run({"status": "generating"})
    assert fake_mlflow.runs[run_id]["status"] == "FAILED"


def test_tracking_failure_is_swallowed(monkeypatch, fake_mlflow):
    def broken(*a, **k):
        raise ConnectionError("tracking server down")

    monkeypatch.setattr(fake_mlflow, "set_experiment", broken)
    with generation_scope("g"):
        assert _start("g") == ""
        MLflowLogger.log_agent_call(agent_name="chef", duration=1.0, model="m",
                                    success=True)


# --------------------------------------------------------------------------
# The real pipeline: per-dish SousChefs on pool threads
# --------------------------------------------------------------------------

def test_real_pipeline_run_logs_into_exactly_one_run(fake_mlflow, monkeypatch):
    from app.agents import plan_pipeline
    from app.db.database import Database
    from app.tests import test_plan_pipeline as tp

    def no_db(*a, **k):
        raise AssertionError("touched the database")

    monkeypatch.setattr(Database, "initialize", no_db)
    monkeypatch.setattr(Database, "get_connection", no_db)

    model = tp.Model(tp.PLAN, {"Chicken with potatoes": [tp.recipe_json("C", [1, 5], [50])],
                               "Tofu broccoli stir-fry": [tp.recipe_json("T", [3, 4], [51])]})
    threads = set()
    real = MLflowLogger.log_agent_call

    def spy(**kw):
        threads.add(threading.get_ident())
        return real(**kw)

    monkeypatch.setattr(MLflowLogger, "log_agent_call", staticmethod(spy))
    deps = plan_pipeline.PlanDeps(
        fetch_deals=lambda pc, r: [dict(d) for d in tp.DEALS],
        fetch_labels=lambda ids: {i: tp.LABELS[i] for i in ids if i in tp.LABELS},
        fetch_pantry=lambda: [dict(x) for x in tp.STAPLES],
        fetch_deal_products=lambda ids: tp.DEAL_PRODUCTS,
        recipe_store=plan_pipeline.InMemoryRecipeStore(), tracker=MLflowLogger,
        chat_fn=model, planner_spec="ollama:big", writer_spec="ollama:mid")
    result = plan_pipeline.run_plan_generation(
        user_id=1, postal_code="L4G3E8", household_size=2, dietary_restrictions=[],
        num_meals=2, deps=deps)

    assert result["status"] == "completed", result["errors"]
    # The writers really ran on pool threads, not the caller's thread.
    assert len(threads) >= 2
    assert len(fake_mlflow.runs) == 1, list(fake_mlflow.runs)
    (run_id, run), = fake_mlflow.runs.items()
    assert run["tags"]["generation_id"] == result["generation_id"]
    assert run["status"] == "FINISHED"
    metrics = run["metrics"]
    assert metrics["chef_total_tokens"] == [150]
    for i in (1, 2):
        assert metrics[f"sous_chef_{i}_total_tokens"] == [120]
    assert "final_recipe_count" in metrics
    for artifact in ("approved_recipes.json", "dish_plan.json", "agent_call_log.json"):
        assert artifact in run["artifacts"]
    assert run["params"]["model_chef"] == "big" and run["params"]["provider_sous_chef"] == "ollama"
    uuid.UUID(result["generation_id"])
    json.dumps(run["params"])  # plain values
