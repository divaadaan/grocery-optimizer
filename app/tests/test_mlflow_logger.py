"""MLflow logging: one run per generation across threads, honest tokens.

A fake ``mlflow`` module records what the logger does. Nothing reaches a
tracking server. The graph-level test drives the real LangGraph run (its
parallel SousChef tasks execute on pool threads) with the real
``MLflowLogger`` as tracker.
"""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.agents.llm_usage import MeteredLLM
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
    return fake


MODELS = {"chef": {"provider": "ollama", "model": "big"},
          "sous_chef": {"provider": "ollama", "model": "mid"},
          "nutritionist": {"provider": "ollama", "model": "small"}}


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
    assert run["params"]["model_nutritionist"] == "small"
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
# MeteredLLM
# --------------------------------------------------------------------------

class Replies:
    def __init__(self, *responses):
        self.responses = list(responses)

    def invoke(self, messages):
        return self.responses.pop(0)


def test_metered_llm_sums_reported_usage_across_retries():
    llm = MeteredLLM(Replies(
        AIMessage(content="bad", usage_metadata={"input_tokens": 10, "output_tokens": 2,
                                                 "total_tokens": 12}),
        AIMessage(content="ok", usage_metadata={"input_tokens": 20, "output_tokens": 3,
                                                "total_tokens": 23})))
    llm.invoke([]), llm.invoke([])
    assert llm.usage == {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}


def test_metered_llm_reports_nothing_when_usage_absent_or_partial():
    assert MeteredLLM(Replies()).usage is None
    none = MeteredLLM(Replies(AIMessage(content="x")))
    none.invoke([])
    assert none.usage is None
    partial = MeteredLLM(Replies(
        AIMessage(content="a", usage_metadata={"input_tokens": 1, "output_tokens": 1,
                                               "total_tokens": 2}),
        AIMessage(content="b")))
    partial.invoke([]), partial.invoke([])
    assert partial.usage is None  # an undercount is not reported as a count


# --------------------------------------------------------------------------
# The real graph: parallel SousChefs on LangGraph's pool threads
# --------------------------------------------------------------------------

class WithUsage:
    """Wraps a scripted chat model; every reply reports provider usage."""

    def __init__(self, inner, tokens):
        self.inner = inner
        self.tokens = tokens

    def invoke(self, messages):
        reply = self.inner.invoke(messages)
        return AIMessage(content=reply.content,
                         usage_metadata={"input_tokens": self.tokens,
                                         "output_tokens": 1,
                                         "total_tokens": self.tokens + 1})


def test_real_graph_run_logs_into_exactly_one_run(fake_mlflow, monkeypatch):
    from app.db.database import Database
    from app.tests.test_graph_offline import _run, make_deps

    def no_db(*a, **k):
        raise AssertionError("touched the database")

    monkeypatch.setattr(Database, "initialize", no_db)
    monkeypatch.setattr(Database, "get_connection", no_db)

    deps = make_deps()
    deps.tracker = MLflowLogger
    deps.chef_llm = WithUsage(deps.chef_llm, 100)
    deps.sous_chef_llm = WithUsage(deps.sous_chef_llm, 40)
    threads = set()
    real = MLflowLogger.log_agent_call

    def spy(**kw):
        threads.add(threading.get_ident())
        return real(**kw)

    monkeypatch.setattr(MLflowLogger, "log_agent_call", staticmethod(spy))

    state = _run(deps)

    assert state["status"] == "completed", state["errors"]
    # The agent calls really came from more than one thread (the pool).
    assert len(threads) >= 2
    assert len(fake_mlflow.runs) == 1, list(fake_mlflow.runs)
    (run_id, run), = fake_mlflow.runs.items()
    assert state["mlflow_run_id"] == run_id
    assert run["tags"]["generation_id"] == state["generation_id"]
    assert run["status"] == "FINISHED"
    metrics = run["metrics"]
    assert metrics["chef_orchestrator_total_tokens"] == [101]
    for i in (1, 2, 3):
        assert metrics[f"sous_chef_{i}_total_tokens"] == [41]
    assert "final_recipe_count" in metrics
    assert "approved_recipes.json" in run["artifacts"]
    uuid.UUID(state["generation_id"])
    json.dumps(run["params"])  # plain values
