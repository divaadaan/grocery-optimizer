"""Infrastructure failures fail fast; model-output failures still retry.

Owner decision (2026-11-23): when the SousChefs come back empty, keep topping
up the shortfall for model-output failures (schema / validation), but go
straight to ``handle_failure`` on infrastructure errors (Ollama unreachable,
connection refused, timeouts, HTTP 5xx) instead of burning retry iterations
against a dead model.

* ``llm_errors.classify`` decides by exception type and its cause/context
  chain only, and anything unknown stays model output (retryable);
* the real chat-model stack (LangChain -> ollama -> httpx) is driven through
  an ``httpx.MockTransport``, so the exception types classified are the ones
  it really raises -- no network;
* the real compiled graph runs offline with scripted models that *raise*
  those types (the ``test_graph_offline`` / ``test_orchestration_core``
  fakes): nothing reaches Postgres, MLflow, a network or a live model.
"""
import json
import socket
import threading

import httpx
import ollama
import pytest
from pydantic import BaseModel, ValidationError

from app.agents import graph as graph_mod
from app.agents import llm_errors
from app.agents.llm_errors import (
    INFRASTRUCTURE,
    MODEL_OUTPUT,
    classify,
    infra_record,
    infrastructure_cause,
)
from app.agents.llm_output import LLMOutputError
from app.tests.test_graph_offline import (  # noqa: F401  (autouse fixture)
    ScriptedChatModel,
    no_database_no_mlflow,
)
from app.tests.test_orchestration_core import (
    RETRY_MARKER,
    THREE_GROUPS,
    _TARGET,
    make_deps,
    offered_in,
    run,
    sous_reply,
)

_REQUEST = httpx.Request("POST", "http://ollama.test/api/chat")


def _status_error(code):
    return httpx.HTTPStatusError(
        f"{code}", request=_REQUEST, response=httpx.Response(code, request=_REQUEST))


def _validation_error():
    class M(BaseModel):
        x: int
    try:
        M.model_validate({"x": "not an int"})
    except ValidationError as e:
        return e
    raise AssertionError("expected a ValidationError")


def _json_error():
    try:
        json.loads("{nope")
    except json.JSONDecodeError as e:
        return e
    raise AssertionError("expected a JSONDecodeError")


def _raised_from(outer, inner):
    """``outer`` raised with ``raise outer from inner``."""
    try:
        raise outer from inner
    except type(outer) as e:
        return e


def _raised_during(outer, inner):
    """``outer`` raised while handling ``inner`` (implicit __context__)."""
    try:
        try:
            raise inner
        except type(inner):
            raise outer
    except type(outer) as e:
        return e


# --------------------------------------------------------------------------
# The classifier
# --------------------------------------------------------------------------

INFRA_CASES = [
    ("connect refused (httpx)", httpx.ConnectError("refused", request=_REQUEST)),
    ("connect timeout", httpx.ConnectTimeout("timed out", request=_REQUEST)),
    ("read timeout", httpx.ReadTimeout("timed out", request=_REQUEST)),
    ("pool timeout", httpx.PoolTimeout("timed out")),
    ("server disconnected", httpx.RemoteProtocolError("Server disconnected")),
    ("read error", httpx.ReadError("reset")),
    ("ollama non-stream connect", ConnectionError("Failed to connect to Ollama")),
    ("socket refused", ConnectionRefusedError(111, "Connection refused")),
    ("socket reset", ConnectionResetError(104, "reset")),
    ("builtin timeout", TimeoutError("timed out")),
    ("dns", socket.gaierror(-2, "Name or service not known")),
    ("ollama 500", ollama.ResponseError("boom", 500)),
    ("ollama 503", ollama.ResponseError("busy", 503)),
    ("httpx 502", _status_error(502)),
    ("wrapped by __cause__",
     _raised_from(RuntimeError("chain failed"), httpx.ConnectError("refused"))),
    ("wrapped by __context__",
     _raised_during(RuntimeError("cleanup failed"), httpx.ReadTimeout("slow"))),
    ("wrapped twice",
     _raised_from(ValueError("outer"),
                  _raised_from(RuntimeError("mid"), socket.gaierror(-2, "dns")))),
]

MODEL_OUTPUT_CASES = [
    ("pydantic ValidationError", _validation_error()),
    ("LLMOutputError", LLMOutputError("RecipeBatch: no valid output", ["{}"], "bad")),
    ("JSONDecodeError", _json_error()),
    ("ValueError", ValueError("bad")),
    ("KeyError", KeyError("recipes")),
    ("unknown RuntimeError", RuntimeError("something")),
    ("ollama 404 (model not found)", ollama.ResponseError("model 'x' not found", 404)),
    ("ollama 400", ollama.ResponseError("bad request", 400)),
    ("ollama mid-stream error, no status", ollama.ResponseError("oops")),
    ("httpx 422", _status_error(422)),
    # An explicit model-output error is model output even if it was raised
    # while some infrastructure error was being handled.
    ("LLMOutputError over a ConnectError",
     _raised_during(LLMOutputError("never validated"), httpx.ConnectError("x"))),
    # Message text never decides: a ValueError *saying* "connection refused".
    ("message text only", ValueError("Connection refused: timed out (503)")),
]


@pytest.mark.parametrize("label,exc", INFRA_CASES, ids=[c[0] for c in INFRA_CASES])
def test_infrastructure_errors_are_classified_as_infrastructure(label, exc):
    assert classify(exc) == INFRASTRUCTURE
    assert llm_errors.is_infrastructure_error(exc)


@pytest.mark.parametrize("label,exc", MODEL_OUTPUT_CASES,
                         ids=[c[0] for c in MODEL_OUTPUT_CASES])
def test_model_output_and_unknown_errors_stay_retryable(label, exc):
    assert classify(exc) == MODEL_OUTPUT
    assert infrastructure_cause(exc) is None


def test_the_cause_found_is_the_outermost_infrastructure_exception():
    # httpx.ConnectError (not its httpcore / socket causes) names the failure.
    inner = ConnectionRefusedError(111, "Connection refused")
    connect = _raised_from(httpx.ConnectError("refused"), inner)
    wrapped = _raised_from(RuntimeError("x"), connect)
    assert infrastructure_cause(wrapped) is connect


def test_a_cyclic_chain_terminates():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__context__, b.__context__ = b, a
    assert classify(a) == MODEL_OUTPUT


# --------------------------------------------------------------------------
# What the real chat-model stack raises (no network: httpx.MockTransport)
# --------------------------------------------------------------------------

def _real_chat_model(handler):
    from langchain_ollama import ChatOllama  # the provider, as make_llm builds it
    return ChatOllama(model="m", base_url="http://ollama.test", format="json",
                      client_kwargs={"transport": httpx.MockTransport(handler)})


def _raise(exc):
    def handler(request):
        raise exc
    return handler


@pytest.mark.parametrize("handler,expected_type", [
    (_raise(httpx.ConnectError("[Errno 111] Connection refused")), httpx.ConnectError),
    (_raise(httpx.ConnectError("[Errno -2] Name or service not known")),
     httpx.ConnectError),
    (_raise(httpx.ReadTimeout("timed out")), httpx.ReadTimeout),
    (lambda request: httpx.Response(503, json={"error": "server busy"}),
     ollama.ResponseError),
    (lambda request: httpx.Response(500, json={"error": "llama runner crashed"}),
     ollama.ResponseError),
])
def test_the_installed_stack_raises_what_the_classifier_calls_infrastructure(
        handler, expected_type):
    from langchain_core.messages import HumanMessage
    with pytest.raises(Exception) as info:
        _real_chat_model(handler).invoke([HumanMessage(content="hi")])
    assert type(info.value) is expected_type
    assert classify(info.value) == INFRASTRUCTURE


def test_the_installed_stack_404_is_not_infrastructure():
    from langchain_core.messages import HumanMessage
    llm = _real_chat_model(lambda r: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ollama.ResponseError) as info:
        llm.invoke([HumanMessage(content="hi")])
    assert classify(info.value) == MODEL_OUTPUT


# --------------------------------------------------------------------------
# The real graph, offline
# --------------------------------------------------------------------------

def _connect_error():
    return httpx.ConnectError("[Errno 111] Connection refused", request=_REQUEST)


def _always(exc_factory):
    def respond(prompt):
        raise exc_factory()
    return respond


def _failing_when(offered_deal_id, ok=None, exc_factory=_connect_error):
    """SousChef reply that raises for the group offering ``offered_deal_id``
    and otherwise answers like ``sous_reply()``. Groups are disjoint, so the
    failing chef is chosen by what it is offered, not by call order."""
    ok = ok or sous_reply()

    def respond(prompt):
        if any(d["deal_id"] == offered_deal_id for d in offered_in(prompt)):
            raise exc_factory()
        return ok(prompt)
    return respond


def _generation(deps, state):
    return deps.recipe_store.generations[state["generation_id"]]


def test_every_sous_chef_unreachable_fails_on_the_first_pass():
    deps = make_deps(THREE_GROUPS, sous=_always(_connect_error))
    nodes = []
    state = run(deps, num_meals=3, progress=nodes.append)

    assert state["status"] == "failed"
    assert state["iteration_count"] == 0  # no retry iteration was spent
    assert "handle_rejections" not in nodes and "retry_generation" not in nodes
    # One call per SousChef: invoke_validated does not re-ask a dead model.
    assert len(deps.sous_chef_llm.prompts) == 3
    infra = state["infrastructure_errors"]
    assert sorted(r["agent"] for r in infra) == ["sous_chef_1", "sous_chef_2", "sous_chef_3"]
    assert {r["error_type"] for r in infra} == {"ConnectError"}
    assert {r["stage"] for r in infra} == {"generate"}

    gen = _generation(deps, state)
    assert gen["status"] == "failed" and gen["num_recipes_saved"] == 0
    assert gen["iteration_count"] == 0
    reason = gen["failure_reason"]
    assert reason.startswith("Infrastructure failure: ConnectError from sous_chef_")
    assert "(model unreachable)" in reason
    assert "0/3 approved after 0 retry iteration(s) (max 2)" in reason
    # The infra reason reaches the job's errors too, not only the DB row.
    assert reason in state["errors"]
    assert any(w.startswith("Model unreachable: ConnectError from sous_chef_")
               for w in state["warnings"])
    # Surfaced to MLflow like the other agent calls (success=False + error).
    assert "log_agent_call" in deps.tracker.calls


@pytest.mark.parametrize("exc_factory,name", [
    (lambda: httpx.ReadTimeout("timed out"), "ReadTimeout"),
    (lambda: ollama.ResponseError("busy", 503), "ResponseError"),
    (lambda: socket.gaierror(-2, "Name or service not known"), "gaierror"),
])
def test_other_infrastructure_errors_also_fail_fast(exc_factory, name):
    deps = make_deps(THREE_GROUPS, sous=_always(exc_factory))
    state = run(deps, num_meals=3)
    assert state["status"] == "failed"
    assert state["iteration_count"] == 0
    assert _generation(deps, state)["failure_reason"].startswith(
        f"Infrastructure failure: {name} from sous_chef_")


def test_model_output_failures_still_retry_up_to_max_iterations():
    # Every SousChef answers with no recipe at all: a validation failure.
    deps = make_deps(THREE_GROUPS, sous=sous_reply(short=99))
    state = run(deps, num_meals=3)

    assert state["status"] == "failed"
    assert state["iteration_count"] == state["max_iterations"] == 2
    assert "infrastructure_errors" not in state
    assert _generation(deps, state)["failure_reason"].startswith(
        "Insufficient approved recipes: 0/3")


def test_a_healthy_run_has_no_infrastructure_errors_channel():
    state = run(make_deps(THREE_GROUPS), num_meals=3)
    assert state["status"] == "completed"
    assert "infrastructure_errors" not in state  # absent, not [] (eval replays)


def test_mixed_enough_approved_stops_retrying_and_finalizes_partial():
    # sous_chef_3 (deals 5, 6) is unreachable; 1 and 2 return their recipe.
    deps = make_deps(THREE_GROUPS, sous=_failing_when(5))
    nodes = []
    state = run(deps, num_meals=3, progress=nodes.append)

    assert state["status"] == "completed"
    assert state["iteration_count"] == 0  # 2/3 >= 60%: no retry against a dead model
    assert "retry_generation" not in nodes
    assert len(state["approved_recipe_ids"]) == 2
    assert "Only 2/3 recipes approved" in state["warnings"]
    assert "Model unreachable: ConnectError from sous_chef_3 (generate)" in state["warnings"]
    gen = _generation(deps, state)
    assert gen["status"] == "partial" and gen["failure_reason"] is None
    assert gen["num_recipes_saved"] == 2


def test_mixed_too_few_approved_fails_without_retrying():
    # Only sous_chef_1 (deals 1, 2) answers: 1/3 < 60%.
    def respond(prompt):
        if not any(d["deal_id"] == 1 for d in offered_in(prompt)):
            raise _connect_error()
        return sous_reply()(prompt)

    deps = make_deps(THREE_GROUPS, sous=respond)
    state = run(deps, num_meals=3)

    assert state["status"] == "failed"
    assert state["iteration_count"] == 0
    reason = _generation(deps, state)["failure_reason"]
    assert reason.startswith("Infrastructure failure: ConnectError from sous_chef_")
    assert "; also ConnectError from sous_chef_" in reason
    assert "1/3 approved after 0 retry iteration(s)" in reason


def test_mixed_with_all_requested_approved_still_finalizes():
    # sous_chef_3 is unreachable, but the others over-deliver to num_meals.
    deps = make_deps(THREE_GROUPS, sous=_failing_when(5, ok=sous_reply(extra=1)))
    state = run(deps, num_meals=3)
    assert state["status"] == "completed"
    assert len(state["saved_recipe_ids"]) == 3
    assert _generation(deps, state)["status"] == "completed"


def test_a_model_that_dies_during_the_retry_stops_the_loop():
    """First pass: sous_chef_3 emits garbage (model output -> retry). On the
    shortfall top-up the model is gone: the run settles at iteration 1 instead
    of spending iteration 2."""
    lock = threading.Lock()
    calls = {"top_up": 0}

    def respond(prompt):
        deals = offered_in(prompt)
        ids = {d["deal_id"] for d in deals}
        target = int(_TARGET.search(prompt).group(1)) if _TARGET.search(prompt) else 0
        if 5 in ids:
            return "not json at all"
        if 1 in ids and target == 1:  # the top-up; first pass asked group 1 for 2
            with lock:
                calls["top_up"] += 1
            raise _connect_error()
        assert RETRY_MARKER not in prompt
        return sous_reply()(prompt)

    deps = make_deps(THREE_GROUPS, sous=respond)
    state = run(deps, num_meals=4)  # targets [2, 1, 1]

    assert calls["top_up"] == 1
    assert state["iteration_count"] == 1
    assert state["status"] == "completed"  # 3/4 approved >= 60%: partial
    assert len(state["approved_recipe_ids"]) == 3
    assert [r["stage"] for r in state["infrastructure_errors"]] == ["generate"]
    assert _generation(deps, state)["status"] == "partial"


def test_planning_infrastructure_failure_is_named_in_the_reason():
    deps = make_deps(THREE_GROUPS)
    deps.chef_llm = ScriptedChatModel(respond=_always(_connect_error), prompts=[])
    state = run(deps, num_meals=3)

    assert state["status"] == "failed"
    assert len(deps.chef_llm.prompts) == 1  # not re-asked
    assert _generation(deps, state)["failure_reason"].startswith(
        "Infrastructure failure: ConnectError from chef_orchestrator (model unreachable)")


def test_an_unreachable_nutritionist_never_decides_compliance_or_fails_the_run():
    """The Nutritionist's model is advisory: when it is unreachable the
    deterministic verdict stands, the run completes, and the dead model is
    asked once per validation pass, not once per recipe."""
    deps = make_deps(THREE_GROUPS)
    deps.nutritionist_llm = ScriptedChatModel(respond=_always(_connect_error), prompts=[])
    state = run(deps, num_meals=3)

    assert state["status"] == "completed"
    assert len(state["approved_recipe_ids"]) == 3
    assert len(deps.nutritionist_llm.prompts) == 1
    assert "infrastructure_errors" not in state
    assert ("Nutrition facts unavailable: model unreachable "
            "(ConnectError from nutritionist)") in state["warnings"]


# --------------------------------------------------------------------------
# Routing and reason, as units
# --------------------------------------------------------------------------

def _route_state(approved, iteration=0, infra=(), num_meals=3):
    return {"approved_recipe_ids": [f"a{i}" for i in range(approved)],
            "rejected_recipe_ids": [], "num_meals": num_meals,
            "iteration_count": iteration, "max_iterations": 2,
            **({"infrastructure_errors": list(infra)} if infra else {})}


DEAD = infra_record("sous_chef_2", httpx.ConnectError("refused"), "generate")


@pytest.mark.parametrize("approved,infra,expected", [
    (0, (), "retry"),
    (0, (DEAD,), "handle_failure"),
    (1, (DEAD,), "handle_failure"),
    (2, (DEAD,), "finalize_partial"),
    (3, (DEAD,), "finalize"),
])
def test_route_after_validation_with_infrastructure_errors(approved, infra, expected):
    assert graph_mod.route_after_validation(_route_state(approved, infra=infra)) == expected


def test_failure_reason_names_the_type_and_agent_and_keeps_the_constraint():
    state = {"num_meals": 3, "approved_recipe_ids": [], "iteration_count": 0,
             "max_iterations": 2, "ingredient_groups": [[{}]],
             "infrastructure_errors": [DEAD, DEAD]}
    reason = graph_mod.failure_reason(state)
    assert reason == ("Infrastructure failure: ConnectError from sous_chef_2 "
                      "(model unreachable) -- 0/3 approved after 0 retry "
                      "iteration(s) (max 2)")
    assert reason  # chk_recipe_generations_failure: failed <=> reason IS NOT NULL
