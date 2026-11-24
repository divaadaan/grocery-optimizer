"""Why an LLM call failed: a dead model, or bad model output.

Owner decision (2026-11-23): retry *model-output* failures (schema /
validation -- often transient), but fail fast on *infrastructure* failures
(model unreachable, connection refused, timeouts, server errors) instead of
burning retries against a dead model. ``plan_pipeline`` stops all retries on
the first infrastructure failure; ``deal_labeller`` stops its run.

:func:`classify` is the one place that decides. It looks at exception
**types** only -- the exception and its ``__cause__`` / ``__context__``
chain -- never at message text. Anything it does not recognise is
``MODEL_OUTPUT``: retryable.

Every model call goes through ``app/agents/llm_chat.py`` (plain ``httpx``,
with an explicit timeout), so what it raises is:

* connection refused / DNS failure -> ``httpx.ConnectError``
  (cause ``ConnectionRefusedError`` / ``socket.gaierror``);
* timeouts -> ``httpx.ConnectTimeout`` / ``httpx.ReadTimeout``;
* an HTTP error status -> ``httpx.HTTPStatusError``. 5xx is infrastructure,
  and so are 404 (model not pulled / wrong name), 401/403 (credentials), 402
  (HF router: no credits) and 429 (rate limit): retrying the same call
  straight away can never succeed. Other 4xx stay model output. Decided by
  status code, never by message text.
* :class:`~app.agents.llm_chat.ChatOutputError` -> no schema-valid reply:
  model output.
"""
from __future__ import annotations

import json
import socket
from typing import Iterator, Optional, Tuple, Type

import httpx
from pydantic import ValidationError

from .llm_chat import ChatOutputError

INFRASTRUCTURE = "infrastructure"
MODEL_OUTPUT = "model_output"

#: Model not pulled / wrong name (404), bad or missing credentials (401/403),
#: no pre-paid credits on the HF router (402), rate limited (429).
_NOT_RETRYABLE_4XX = frozenset({401, 402, 403, 404, 429})

#: Raised when the model could not be reached or did not answer in time.
#: ``httpx.TransportError`` covers ConnectError, the timeouts, ReadError,
#: RemoteProtocolError, ...; ``TimeoutError`` is also ``asyncio.TimeoutError``.
INFRASTRUCTURE_TYPES: Tuple[Type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    socket.gaierror,
    httpx.TransportError,
)

#: The model answered, but not usefully. Decided as model output even when
#: raised while handling something else (their ``__context__`` is ignored).
MODEL_OUTPUT_TYPES: Tuple[Type[BaseException], ...] = (
    ChatOutputError,
    ValidationError,
    json.JSONDecodeError,
)


def _is_infrastructure(exc: BaseException) -> bool:
    if isinstance(exc, INFRASTRUCTURE_TYPES):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = getattr(exc.response, "status_code", None)
        return isinstance(code, int) and (code >= 500 or code in _NOT_RETRYABLE_4XX)
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
