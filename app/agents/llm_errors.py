"""Why an LLM call failed: a dead model, or bad model output.

Owner decision (2026-11-23): when SousChefs come back empty, keep topping up
the shortfall for *model-output* failures (schema / validation -- often
transient), but fail fast to ``handle_failure`` on *infrastructure* failures
(Ollama unreachable, connection refused, timeouts, server errors) instead of
burning retry iterations against a dead model.

:func:`classify` is the one place that decides. It looks at exception
**types** only -- the exception and its ``__cause__`` / ``__context__``
chain -- never at message text. Anything it does not recognise is
``MODEL_OUTPUT``: retryable, which is the behaviour before this module.

What the installed stack raises (langchain-ollama 0.2.3 -> ollama 0.6.2 ->
httpx 0.28.1), observed against local test servers:

* connection refused -> ``httpx.ConnectError`` (cause chain
  ``httpcore.ConnectError`` -> ``ConnectionRefusedError``). The LangChain
  chat model streams, so ollama's streaming path lets httpx's error through; its
  non-streaming path re-raises it as the builtin ``ConnectionError``.
* DNS failure        -> ``httpx.ConnectError`` (-> ``socket.gaierror``)
* read timeout       -> ``httpx.ReadTimeout`` (-> ``httpcore.ReadTimeout``
  -> ``TimeoutError``); connect timeouts are ``httpx.ConnectTimeout``.
* HTTP 5xx           -> ``ollama.ResponseError`` with ``status_code`` 500+
  (context ``httpx.HTTPStatusError``).
* HTTP 404           -> the same types with ``status_code`` 404: the model is
  not pulled or the name is wrong. Retrying can never succeed, so it is
  infrastructure (decided by status code, never by message text). Other 4xx
  and the status-less ``ResponseError`` an error line mid-stream raises stay
  model output.

The timeouts above only exist because ``llm_factory`` sets them explicitly:
the Ollama chat client has no default HTTP timeout, so a hung server would block forever.
"""
from __future__ import annotations

import json
import socket
from typing import Iterator, Optional, Tuple, Type

from pydantic import ValidationError

from .llm_output import LLMOutputError

INFRASTRUCTURE = "infrastructure"
MODEL_OUTPUT = "model_output"

#: Model not pulled / wrong name: can never succeed on retry.
_NOT_FOUND = 404


def _optional_types() -> Tuple[Tuple[Type[BaseException], ...], Tuple[type, ...]]:
    """(transport error types, HTTP-status error types) from httpx / ollama,
    when installed. Imported defensively: the classifier must not make a
    provider package a hard dependency of the agents."""
    transport: Tuple[Type[BaseException], ...] = ()
    status: Tuple[type, ...] = ()
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with the stack
        pass
    else:
        # ConnectError, ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout,
        # ReadError, WriteError, RemoteProtocolError, ProxyError, ...
        transport += (httpx.TransportError,)
        status += (httpx.HTTPStatusError,)
    try:
        import ollama
    except ImportError:  # pragma: no cover
        pass
    else:
        status += (ollama.ResponseError,)
    return transport, status


_TRANSPORT_TYPES, _STATUS_TYPES = _optional_types()

#: Raised when the model could not be reached or did not answer in time.
#: ``ConnectionError`` covers refused / reset / aborted and ollama's own
#: re-raise; ``TimeoutError`` is also ``asyncio.TimeoutError`` on 3.11.
INFRASTRUCTURE_TYPES: Tuple[Type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
) + _TRANSPORT_TYPES

#: The model answered, but not usefully. Decided as model output even when
#: raised while handling something else (their ``__context__`` is ignored).
MODEL_OUTPUT_TYPES: Tuple[Type[BaseException], ...] = (
    LLMOutputError,
    ValidationError,
    json.JSONDecodeError,
)


def _status_code(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "status_code", None)  # ollama.ResponseError
    if code is None:
        response = getattr(exc, "response", None)  # httpx.HTTPStatusError
        code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def _is_infrastructure(exc: BaseException) -> bool:
    if isinstance(exc, INFRASTRUCTURE_TYPES):
        return True
    if _STATUS_TYPES and isinstance(exc, _STATUS_TYPES):
        code = _status_code(exc)
        return code is not None and (code >= 500 or code == _NOT_FOUND)
    return False


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and everything reachable through ``__cause__`` / ``__context__``,
    each once (chains can be cyclic)."""
    seen = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        stack.extend((e.__context__, e.__cause__))


def infrastructure_cause(exc: BaseException) -> Optional[BaseException]:
    """The infrastructure exception behind ``exc``, or ``None`` if ``exc`` is
    (or must be treated as) a model-output failure."""
    if isinstance(exc, MODEL_OUTPUT_TYPES):
        return None
    for e in _chain(exc):
        if _is_infrastructure(e):
            return e
    return None


def classify(exc: BaseException) -> str:
    """``INFRASTRUCTURE`` or ``MODEL_OUTPUT`` (the default for the unknown)."""
    return INFRASTRUCTURE if infrastructure_cause(exc) is not None else MODEL_OUTPUT


def is_infrastructure_error(exc: BaseException) -> bool:
    return infrastructure_cause(exc) is not None


class LLMInfrastructureError(RuntimeError):
    """An agent's model could not be reached. Raised by an agent method so its
    graph node can record it (:func:`infra_record`) instead of the failure
    reading as "the model returned nothing"."""

    def __init__(self, agent: str, cause: BaseException, stage: str = ""):
        self.agent = agent
        self.cause = cause
        self.stage = stage
        super().__init__(f"{agent}: {type(cause).__name__}: {cause}")


def infra_record(agent: str, cause: BaseException, stage: str) -> dict:
    """One entry of the ``infrastructure_errors`` state channel."""
    return {
        "agent": agent,
        "stage": stage,
        "error_type": type(cause).__name__,
        "message": str(cause)[:300],
    }


def describe(record: dict) -> str:
    """``ConnectError from sous_chef_2 (model unreachable)``."""
    return f"{record['error_type']} from {record['agent']} (model unreachable)"
