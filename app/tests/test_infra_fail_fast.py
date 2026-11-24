"""Infrastructure failures fail fast; model-output failures still retry.

Owner decision (2026-11-23): when the SousChefs come back empty, keep topping
up the shortfall for model-output failures (schema / validation), but go
straight to the failure path on infrastructure errors (Ollama unreachable,
connection refused, timeouts, HTTP 5xx) instead of burning retry iterations
against a dead model.

* ``llm_errors.classify`` decides by exception type and its cause/context
  chain only, and anything unknown stays model output (retryable).

What the real chat client raises is tested in ``test_llm_chat`` (through an
``httpx.MockTransport``), and the pipeline's fail-fast in
``test_plan_pipeline``.
"""
import json
import socket
import threading

import httpx
import ollama
import pytest
from pydantic import BaseModel, ValidationError

from app.agents import llm_errors
from app.agents.llm_errors import (
    INFRASTRUCTURE,
    MODEL_OUTPUT,
    classify,
    infra_record,
    infrastructure_cause,
)
from app.agents.llm_output import LLMOutputError

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
    ("ollama 404 (model not found)", ollama.ResponseError("model 'x' not found", 404)),
    ("httpx 404", _status_error(404)),
    # Credentials / HF router credits / rate limit: an immediate retry of the
    # same call cannot succeed (llm_chat's HF calls raise these).
    ("httpx 401", _status_error(401)),
    ("httpx 402 (no HF credits)", _status_error(402)),
    ("ollama 403", ollama.ResponseError("forbidden", 403)),
    ("httpx 429", _status_error(429)),
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
    ("ollama 400", ollama.ResponseError("bad request", 400)),
    ("ollama mid-stream error, no status", ollama.ResponseError("oops")),
    ("httpx 422", _status_error(422)),
    # Status decides, not text: a 400 *saying* "not found" is model output.
    ("ollama 400 saying not found", ollama.ResponseError("model not found", 400)),
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
