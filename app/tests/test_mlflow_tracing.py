"""MLflow tracking end to end against a temporary ``file://`` store.

No server, no network, no LLM: the real compiled graph runs on the scripted
chat models of ``test_graph_offline`` with the *production* tracker
(``MLflowLogger``), and the tests read back what landed in the store --
the one trace per generation (node spans, every chat-model call with its
prompt and raw reply, the parallel SousChefs included), its link to the run,
``chef_plan.json``, and the outage path (unreachable server -> local
fallback, fast).
"""
import copy
import json
import socket
import time

import mlflow
import pytest
import requests
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.chef_orchestrator import ChefOrchestrator
from app.config import settings
from app.services import mlflow_logger
from app.services.mlflow_logger import MLflowLogger, generation_scope, generation_trace
from app.tests import test_graph_offline as offline

EXPERIMENT = "test-tracing"


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    from app.db.database import Database

    def no_db(*args, **kwargs):
        raise AssertionError("tracing test touched the database")

    monkeypatch.setattr(Database, "initialize", no_db)
    monkeypatch.setattr(Database, "get_connection", no_db)


@pytest.fixture(autouse=True)
def isolated_mlflow(tmp_path, monkeypatch):
    """Tracking into tmp_path; every global MLflow touches is restored."""
    from mlflow.tracking import fluent
    from mlflow.tracking._tracking_service import utils as tracking_utils

    monkeypatch.setattr(tracking_utils, "_tracking_uri", tracking_utils._tracking_uri)
    monkeypatch.setattr(fluent, "_active_experiment_id", fluent._active_experiment_id)
    for key in mlflow_logger.HTTP_ENV_CAPS:
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)  # restored to "unset" at teardown
    monkeypatch.setattr(settings, "mlflow_tracking_uri", (tmp_path / "mlruns").as_uri())
    monkeypatch.setattr(settings, "mlflow_experiment_name", EXPERIMENT)
    monkeypatch.setattr(settings, "mlflow_fallback_tracking_uri",
                        (tmp_path / "fallback").as_uri())
    monkeypatch.setattr(settings, "mlflow_tracing", True)
    mlflow_logger._reset_configuration()
    yield tmp_path
    mlflow_logger._reset_configuration()
    mlflow.tracing.reset()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _traced_deps():
    deps = offline.make_deps()
    deps.tracker = MLflowLogger
    return deps


def _experiment_id():
    return mlflow.get_experiment_by_name(EXPERIMENT).experiment_id


def _traces(**kw):
    return mlflow.search_traces(experiment_ids=[_experiment_id()], return_type="list", **kw)


def _artifact(run_id, path):
    local = mlflow.MlflowClient().download_artifacts(run_id, path)
    with open(local, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Tracing
# --------------------------------------------------------------------------

def test_one_trace_per_generation_linked_to_its_run():
    state = offline._run(_traced_deps())
    assert state["status"] == "completed", state["errors"]
    run_id = state["mlflow_run_id"]
    assert run_id

    traces = _traces()
    assert len(traces) == 1
    trace = traces[0]
    assert trace.info.request_metadata["mlflow.sourceRun"] == run_id
    assert trace.info.tags["generation_id"] == state["generation_id"]
    assert [t.info.request_id for t in _traces(run_id=run_id)] == [trace.info.request_id]

    run = mlflow.MlflowClient().get_run(run_id)
    assert run.data.tags["trace_request_id"] == trace.info.request_id
    assert run.data.tags["generation_id"] == state["generation_id"]

    root = [s for s in trace.data.spans if s.parent_id is None]
    assert [s.name for s in root] == ["recipe_generation"]


def test_spans_cover_every_node_and_the_parallel_sous_chefs_share_the_trace():
    offline._run(_traced_deps())
    (trace,) = _traces()
    spans = trace.data.spans
    by_id = {s.span_id: s for s in spans}
    names = [s.name for s in spans]

    for node in ("initialize_chef", "load_recipe_catalog", "plan_ingredient_groups",
                 "aggregate_recipes", "nutritionist_validate", "finalize_meal_plan"):
        assert node in names, node
    sous = [s for s in spans if s.name.startswith("sous_chef_generate")]
    assert len(sous) == 3  # the three Send() tasks, on pool threads
    assert {s.request_id for s in spans} == {trace.info.request_id}
    # Each SousChef span sits under the graph span and holds its own LLM call.
    graph_span = next(s for s in spans if s.name == "LangGraph")
    for s in sous:
        assert s.parent_id == graph_span.span_id
        llm = [c for c in spans if c.parent_id == s.span_id and c.span_type == "CHAT_MODEL"]
        assert len(llm) == 1

    chef_node = next(s for s in spans if s.name == "plan_ingredient_groups")
    (chef_llm,) = [s for s in spans
                   if s.span_type == "CHAT_MODEL" and s.parent_id == chef_node.span_id]
    assert by_id[chef_llm.parent_id].name == "plan_ingredient_groups"


def test_llm_spans_hold_the_full_prompt_and_raw_reply():
    deps = _traced_deps()
    offline._run(deps)
    (trace,) = _traces()
    chef_node = next(s for s in trace.data.spans if s.name == "plan_ingredient_groups")
    (llm,) = [s for s in trace.data.spans
              if s.span_type == "CHAT_MODEL" and s.parent_id == chef_node.span_id]

    prompt = deps.chef_llm.prompts[0]
    assert json.dumps(prompt)[1:-1] in json.dumps(llm.inputs)  # the whole prompt
    raw_reply = json.dumps(offline.FIXTURE["llm"]["chef"])
    assert llm.outputs["generations"][0][0]["text"] == raw_reply
    assert llm.end_time_ns > llm.start_time_ns  # latency


class _MeteredChat(BaseChatModel):
    """A chat model whose reply carries provider usage, as ChatOllama's does."""

    @property
    def _llm_type(self) -> str:
        return "metered"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = AIMessage(content="raw reply", usage_metadata={
            "input_tokens": 120, "output_tokens": 7, "total_tokens": 127})
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_llm_spans_carry_provider_token_usage():
    with generation_scope("g-usage") as scope, generation_trace(scope) as callbacks:
        _MeteredChat().invoke("hello", config={"callbacks": callbacks})
    (trace,) = _traces()
    (llm,) = [s for s in trace.data.spans if s.span_type == "CHAT_MODEL"]
    usage = {"input_tokens": 120, "output_tokens": 7, "total_tokens": 127}
    assert llm.attributes["token_usage"] == usage
    assert trace.info.tags.get("generation_id") == "g-usage"


def test_tracing_does_not_change_graph_results():
    def summary(state):
        recipes = [state["generated_recipes"][rid] for rid in state["approved_recipe_ids"]]
        return (state["status"], state["iteration_count"], round(state["total_cost"], 2),
                sorted((r["name"], round(r["total_cost"], 2)) for r in recipes))

    traced = offline._run(_traced_deps())
    untraced = offline._run(offline.make_deps())  # injected tracker: no tracing
    assert summary(traced) == summary(untraced)
    assert len(_traces()) == 1  # the untraced run recorded nothing


def test_injected_tracker_gets_no_tracing_and_never_configures_mlflow(monkeypatch):
    def boom():
        raise AssertionError("injected tracker must not configure MLflow")

    monkeypatch.setattr(mlflow_logger, "_ensure_configured", boom)
    state = offline._run(offline.make_deps())
    assert state["status"] == "completed"


def test_generation_trace_is_a_noop_when_mlflow_is_unavailable(monkeypatch):
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", lambda: False)
    with generation_scope("g") as scope, generation_trace(scope) as callbacks:
        assert callbacks == []
        assert mlflow.get_current_active_span() is None


def test_tracing_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "mlflow_tracing", False)
    state = offline._run(_traced_deps())
    assert state["mlflow_run_id"]  # the run is still logged
    assert _traces() == []


def test_a_failing_graph_still_closes_its_trace_and_raises():
    with pytest.raises(RuntimeError, match="graph blew up"):
        with generation_scope("g-fail") as scope, generation_trace(scope) as callbacks:
            assert callbacks
            raise RuntimeError("graph blew up")
    (trace,) = _traces()
    assert trace.info.status == "ERROR"
    assert trace.info.tags["generation_id"] == "g-fail"


# --------------------------------------------------------------------------
# Chef plan artifact
# --------------------------------------------------------------------------

class _Reply:
    def __init__(self, content):
        self.content = content
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}


class _ChefLLM:
    def __init__(self, reply):
        self.reply = reply

    def invoke(self, messages, *args, **kwargs):
        return _Reply(json.dumps(self.reply))


def _chef_state(num_meals=3):
    user = offline.FIXTURE["user"]
    return {
        **user, "num_meals": num_meals, "preferences": {},
        "available_deals": copy.deepcopy(offline.FIXTURE["deals"]),
        "budget_spent_this_period": 0.0, "budget_period": "week",
        "provenance": {},
    }


def test_chef_plan_records_raw_dropped_backfilled_and_final_groups():
    # The model invents two deals and returns no reuse map -- which used to
    # make log_ingredient_groups raise (max of an empty map) and log nothing.
    reply = {
        "ingredient_groups": [
            [{"product_name": "FIRM TOFU 350G", "quantity_estimate": "1", "deal_id": 11},
             {"product_name": "TEA BAGS", "quantity_estimate": "1 box", "deal_id": 999}],
            [{"product_name": "CHICKEN THIGHS", "quantity_estimate": "1 kg", "deal_id": 777}],
            [{"product_name": "BASMATI RICE 2KG", "quantity_estimate": "1", "deal_id": 13}],
        ],
        "ingredient_reuse_map": {},
        "rationale": "why not",
    }
    chef = ChefOrchestrator(llm=_ChefLLM(reply), db=offline.FakeDeals(offline.FIXTURE["deals"]),
                            tracker=MLflowLogger)
    state = _chef_state(num_meals=2)
    with generation_scope("g-chef"):
        state.update(chef.initialize(state))
        update = chef.plan_ingredient_groups(state)
        run_id = state["mlflow_run_id"]
    assert update["status"] == "generating"

    plan = _artifact(run_id, "chef_plan.json")
    assert plan["served_deals_available"] == 6
    assert plan["served_deals_in_prompt"] == 6
    assert plan["prompt_deal_ids"] == [d["deal_id"] for d in offline.FIXTURE["deals"]]
    assert [[s["deal_id"] for s in g] for g in plan["raw_groups"]] == [[11, 999], [777], [13]]
    assert plan["dropped"] == [
        {"group": 0, "product_name": "TEA BAGS", "deal_id": 999,
         "reason": "deal_id 999 was not served"},
        {"group": 1, "product_name": "CHICKEN THIGHS", "deal_id": 777,
         "reason": "deal_id 777 was not served"},
    ]
    assert plan["backfilled"] == [{"group": 1, "deal_id": 12, "product_name": "BROCCOLI CROWNS"}]
    assert [[s["deal_id"] for s in g] for g in plan["restricted_groups"]] == [[11], [12], [13]]
    # num_meals=2: group 2 folds into group 0.
    assert [[s["deal_id"] for s in g] for g in plan["final_groups"]] == [[11, 13], [12]]
    assert plan["final_groups"] == update["ingredient_groups"]
    assert plan["ingredient_reuse_map"] == {}
    assert plan["num_meals"] == 2
    assert plan["rationale"] == "why not"

    metrics = mlflow.MlflowClient().get_run(run_id).data.metrics
    assert metrics["ingredient_groups_count"] == 2
    assert metrics["avg_ingredients_per_group"] == 1.5
    assert metrics["total_unique_ingredients"] == 0
    assert "max_ingredient_reuse" not in metrics  # no map, no fabricated stat
    assert metrics["chef_selections_dropped"] == 2
    assert metrics["chef_selections_backfilled"] == 1
    assert metrics["chef_served_deals_available"] == 6
    assert metrics["chef_served_deals_in_prompt"] == 6
    assert _artifact(run_id, "ingredient_reuse_map.json") == {}


def test_reuse_metrics_logged_when_the_model_gives_a_map():
    with generation_scope("g-reuse"):
        run_id = MLflowLogger.start_run(1, 3, 50.0, [])
        MLflowLogger.log_ingredient_groups([[{"deal_id": 1}], [{"deal_id": 2}]],
                                           {"A": 2, "B": 1})
    metrics = mlflow.MlflowClient().get_run(run_id).data.metrics
    assert metrics["max_ingredient_reuse"] == 2
    assert metrics["avg_ingredient_reuse"] == 1.5
    assert _artifact(run_id, "chef_plan.json") == {"final_groups": [[{"deal_id": 1}],
                                                                    [{"deal_id": 2}]]}


# --------------------------------------------------------------------------
# Outages
# --------------------------------------------------------------------------

def test_unreachable_server_falls_back_to_the_local_store_fast(isolated_mlflow, monkeypatch):
    monkeypatch.setattr(settings, "mlflow_tracking_uri", f"http://127.0.0.1:{_closed_port()}")
    started = time.monotonic()
    assert mlflow_logger._ensure_configured() is True
    assert time.monotonic() - started < 3.0
    assert mlflow.get_tracking_uri() == (isolated_mlflow / "fallback").as_uri()


def test_a_whole_generation_against_a_dead_server_is_fast_and_lands_in_the_fallback(
        isolated_mlflow, monkeypatch):
    monkeypatch.setattr(settings, "mlflow_tracking_uri", f"http://127.0.0.1:{_closed_port()}")
    started = time.monotonic()
    state = offline._run(_traced_deps())
    assert time.monotonic() - started < 10.0
    assert state["status"] == "completed"
    assert state["mlflow_run_id"]
    assert (isolated_mlflow / "fallback").is_dir()
    assert len(_traces(run_id=state["mlflow_run_id"])) == 1


def test_fallback_off_disables_tracking(monkeypatch):
    monkeypatch.setattr(settings, "mlflow_tracking_uri", f"http://127.0.0.1:{_closed_port()}")
    monkeypatch.setattr(settings, "mlflow_fallback_tracking_uri", "off")
    started = time.monotonic()
    assert mlflow_logger._ensure_configured() is False
    assert time.monotonic() - started < 3.0
    state = offline._run(_traced_deps())
    assert state["status"] == "completed"
    assert state["mlflow_run_id"] == ""


def test_http_retry_caps_are_set_without_overriding_explicit_values(monkeypatch):
    monkeypatch.setenv("MLFLOW_HTTP_REQUEST_TIMEOUT", "42")
    mlflow_logger._ensure_configured()
    import os
    assert os.environ["MLFLOW_HTTP_REQUEST_TIMEOUT"] == "42"
    assert os.environ["MLFLOW_HTTP_REQUEST_MAX_RETRIES"] == "1"
    assert os.environ["MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR"] == "1"
    from mlflow.environment_variables import (MLFLOW_HTTP_REQUEST_MAX_RETRIES,
                                              MLFLOW_HTTP_REQUEST_TIMEOUT)
    assert MLFLOW_HTTP_REQUEST_MAX_RETRIES.get() == 1
    assert MLFLOW_HTTP_REQUEST_TIMEOUT.get() == 42


def test_server_dying_mid_run_trips_the_breaker_for_the_rest_of_the_generation(monkeypatch):
    calls = []
    with generation_scope("g-dies") as scope:
        assert MLflowLogger.start_run(1, 3, 50.0, [])

        def dead(*args, **kwargs):
            calls.append(args)
            raise requests.ConnectionError("Connection refused")

        monkeypatch.setattr(mlflow_logger._client, "log_metric", dead)
        MLflowLogger.log_agent_call("Chef", 1.0, "m", True)
        assert scope.tripped
        assert len(calls) == 1

        def must_not_configure():
            raise AssertionError("a tripped generation must not re-probe")

        monkeypatch.setattr(mlflow_logger, "_ensure_configured", must_not_configure)
        MLflowLogger.log_agent_call("SousChef", 1.0, "m", True)
        MLflowLogger.log_final_metrics(1.0, 1.0, None, 0, 1, True)
        assert len(calls) == 1
    # The next generation re-probes from scratch.
    assert mlflow_logger._configured is False


def test_non_connection_errors_do_not_trip_the_breaker(monkeypatch):
    with generation_scope("g-bug") as scope:
        assert MLflowLogger.start_run(1, 3, 50.0, [])
        monkeypatch.setattr(mlflow_logger._client, "log_metric",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad metric")))
        MLflowLogger.log_agent_call("Chef", 1.0, "m", True)
        assert not scope.tripped
