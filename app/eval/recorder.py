"""Record and replay the generation pipeline's LLM calls.

The approach-C agents (``app/agents/plan_pipeline.py``) reach a model through
exactly one surface: a *chat function* with ``llm_chat.chat``'s signature,
``chat_fn(spec, messages, **params) -> Reply``, where ``messages`` is a list
of ``{"role", "content"}`` dicts (the task prompt, then on a retry the bad
reply and the correction request) and only the ``Reply`` is read back. The
wrappers here implement that surface:

* :class:`RecordingChat` -- wraps a real chat function for one role and
  appends every call to a shared :class:`CallLog`: role, the canonical request
  messages, the model spec and call parameters, the reply text, finish reason
  and token usage *as the provider reported it* (absent -> ``None``, never
  estimated), latency, and any exception.
* :class:`ReplayChat` -- serves replies from a recorded call log through a
  :class:`ReplayLog`. No network, no model.

Call matching (why not "the n-th call")
---------------------------------------
The per-dish SousChefs run in parallel threads, so the global order in which
calls reach the model is wall-clock scheduling, not program logic. A replay
that served "the next recorded response" would hand SousChef 2 the reply
recorded for SousChef 1 whenever the threads interleave differently.

Instead every call is keyed by ``(role, request_sha256, occurrence)``:

* ``request_sha256`` -- SHA-256 of the canonical JSON of the request
  messages (:func:`canonical_messages`: each message's ``type`` and
  ``content``, plus ``name``/``additional_kwargs`` only when set);
* ``occurrence`` -- 0 for the first time that role sent that exact request
  in the run, 1 for the second, ...

A request's content already pins down which agent, which deal group, which
recipe and which retry attempt it is, so the key is independent of thread
scheduling. The occurrence counter only matters for byte-identical requests,
which are interchangeable by construction -- with one residual caveat: two
*parallel* SousChefs sent byte-identical prompts (a Chef plan that repeats a
group verbatim) may receive each other's recorded replies. Their requests
are identical, so the swap is only visible as which ``chef_id`` owns which
recipe.

Recipe ids are not printed into any prompt, but they key the final state, and
production mints them with ``uuid4``. :class:`ContentRecipeIds` (injected as
``PlanDeps.recipe_ids``) derives them from the chef id and the validated
recipe instead, so a replay reproduces the recorded state.

The recorded log is kept in the order calls *started* (``seq``), which is
informative but is never used for matching.
"""
from __future__ import annotations

import datetime as _dt
import decimal
import difflib
import hashlib
import json
import math
import threading
import time
import uuid
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: The pipeline's model roles: the dish planner ("chef") and the per-dish
#: recipe writer ("sous_chef"). A call log entry must name one of these.
ROLES = ("chef", "sous_chef")


# --------------------------------------------------------------------------
# JSON conversion
# --------------------------------------------------------------------------

def _note(notes: Optional[list], path: str, what: str) -> None:
    if notes is not None:
        notes.append({"path": path, "note": what})


def jsonable(value: Any, notes: Optional[list] = None, path: str = "$") -> Any:
    """``value`` as plain JSON data, never raising.

    Lossless for what the graph state actually holds (dicts, lists, str,
    int, finite float, bool, None; tuples become lists). Anything lossy is
    converted *and* noted in ``notes`` with its path, so a scorer can tell a
    converted value from a real one:

    * non-string dict keys -> ``str(key)``
    * non-finite float     -> ``{"__float__": "nan" | "inf" | "-inf"}``
    * set / frozenset      -> sorted list
    * pydantic model       -> ``model_dump(mode="json")``
    * date / datetime      -> ISO string; ``Decimal`` -> float
    * anything else        -> ``{"__unserialisable__": type, "repr": ...}``

    Never raising matters for recording: losing a live run's call log to a
    serialisation error at the very end would waste the run.
    """
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        _note(notes, path, "non-finite float")
        return {"__float__": repr(value)}
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = k if isinstance(k, str) else str(k)
            if not isinstance(k, str):
                _note(notes, f"{path}.{key}", f"{type(k).__name__} key stringified")
            out[key] = jsonable(v, notes, f"{path}.{key}")
        return out
    if isinstance(value, (list, tuple)):
        return [jsonable(v, notes, f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, (set, frozenset)):
        _note(notes, path, f"{type(value).__name__} as sorted list")
        items = [jsonable(v, notes, f"{path}[]") for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    if hasattr(value, "model_dump"):
        _note(notes, path, f"pydantic {type(value).__name__} dumped")
        return jsonable(value.model_dump(mode="json"), notes, path)
    if isinstance(value, (_dt.date, _dt.datetime)):
        _note(notes, path, f"{type(value).__name__} as ISO string")
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        _note(notes, path, "Decimal as float")
        return float(value)
    _note(notes, path, f"unserialisable {type(value).__name__}")
    return {"__unserialisable__": type(value).__name__, "repr": repr(value)[:500]}


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------

def canonical_messages(messages: Any) -> List[Dict[str, Any]]:
    """The request as plain data: ``[{"type", "content"[, "name"]
    [, "additional_kwargs"]}, ...]``.

    A bare string (which ``invoke`` also accepts) is a single human message,
    so it hashes the same as ``[HumanMessage(content=s)]``.
    """
    if isinstance(messages, str):
        return [{"type": "human", "content": messages}]
    out = []
    for m in messages:
        if isinstance(m, str):
            out.append({"type": "human", "content": m})
            continue
        if isinstance(m, dict):
            entry = {"type": m.get("type") or m.get("role"), "content": jsonable(m.get("content"))}
        elif isinstance(m, tuple) and len(m) == 2:
            entry = {"type": m[0], "content": jsonable(m[1])}
        else:
            entry = {"type": getattr(m, "type", type(m).__name__),
                     "content": jsonable(getattr(m, "content", None))}
            if getattr(m, "name", None):
                entry["name"] = m.name
            if getattr(m, "additional_kwargs", None):
                entry["additional_kwargs"] = jsonable(m.additional_kwargs)
        out.append(entry)
    return out


def request_sha256(canonical: List[Dict[str, Any]]) -> str:
    text = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_request(canonical: List[Dict[str, Any]]) -> str:
    """Human-readable request text, for mismatch diffs."""
    parts = []
    for i, m in enumerate(canonical):
        content = m.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, sort_keys=True, ensure_ascii=False)
        parts.append(f"=== message {i} ({m.get('type')}) ===\n{content}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

def model_identity(spec: str, inner: Any = None) -> Dict[str, Any]:
    """What a recorded call says about the model that answered it: the spec's
    provider and model (``"ollama:qwen2.5:7b"`` -> ollama / qwen2.5:7b) and the
    chat function that served it. Not validated: a scripted test provider is
    recorded as given."""
    provider, _, name = (spec or "").partition(":")
    target = getattr(inner, "__wrapped__", inner)
    cls = (f"{target.__module__}.{getattr(target, '__qualname__', type(target).__qualname__)}"
           if target is not None else "app.agents.llm_chat.chat")
    return {"provider": provider, "model": name, "class": cls, "params": None}


class CallLog:
    """The run's LLM calls, shared by the three roles' recorders. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._occurrence: Counter = Counter()
        self._calls: List[Dict[str, Any]] = []

    def begin(self, role: str, digest: str) -> Tuple[int, int]:
        """Reserve ``(seq, occurrence)`` for a call that is about to start."""
        with self._lock:
            seq = self._seq
            self._seq += 1
            occurrence = self._occurrence[(role, digest)]
            self._occurrence[(role, digest)] += 1
            return seq, occurrence

    def finish(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self._calls.append(entry)

    @property
    def calls(self) -> List[Dict[str, Any]]:
        """Completed calls in the order they started."""
        with self._lock:
            return sorted((dict(c) for c in self._calls), key=lambda c: c["seq"])


class RecordingChat:
    """A real chat function for one role, with every call logged."""

    def __init__(self, inner: Any, role: str, log: CallLog, spec: str):
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; known: {ROLES}")
        self.inner = inner
        self.role = role
        self.log = log
        self.spec = spec
        self.identity = model_identity(spec, inner)

    def __call__(self, spec: str, messages: Any, **params: Any) -> Any:
        canonical = canonical_messages(messages)
        digest = request_sha256(canonical)
        seq, occurrence = self.log.begin(self.role, digest)
        entry: Dict[str, Any] = {
            "seq": seq, "role": self.role, "request_sha256": digest,
            "occurrence": occurrence, "request": {"messages": canonical},
            "model": {**self.identity, "params": jsonable(params)},
            "response": None, "error": None, "latency_s": None,
        }
        started = time.perf_counter()
        try:
            reply = self.inner(self.spec, messages, **params)
        except Exception as exc:
            entry["latency_s"] = time.perf_counter() - started
            entry["error"] = {"type": type(exc).__name__, "message": str(exc)}
            self.log.finish(entry)
            raise
        entry["latency_s"] = time.perf_counter() - started
        usage = None
        if reply.input_tokens is not None or reply.output_tokens is not None:
            total = (reply.input_tokens or 0) + (reply.output_tokens or 0)
            usage = {"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                     "total_tokens": total}
        entry["response"] = {"content": reply.text, "usage": usage,
                             "response_metadata": {"finish_reason": reply.finish_reason,
                                                   "truncated_prompt": reply.truncated_prompt}}
        self.log.finish(entry)
        return reply


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------

class ReplayMismatch(RuntimeError):
    """A replayed run asked for a call the log does not hold."""


class ReplayLeftover(ReplayMismatch):
    """A strict replay finished with recorded calls never asked for."""


class ReplayedLLMError(RuntimeError):
    """Re-raised in place of an exception the model raised when recorded."""


def call_key(entry: Dict[str, Any]) -> Tuple[str, str, int]:
    return entry["role"], entry["request_sha256"], entry["occurrence"]


class ReplayLog:
    """Serves recorded calls by ``(role, request_sha256, occurrence)``.

    The agents catch exceptions from their model and degrade (a failed plan,
    a skipped SousChef), so a mismatch raised inside ``invoke`` would be
    swallowed into an ordinary-looking failed run. Every mismatch is
    therefore also kept in :attr:`mismatches`, and :meth:`check` -- which the
    runner calls after the graph returns -- re-raises the first one. With
    ``strict``, ``check`` also raises :class:`ReplayLeftover` if any recorded
    call went unused.
    """

    def __init__(self, calls: Iterable[Dict[str, Any]], strict: bool = True):
        self.strict = strict
        self._lock = threading.Lock()
        self._index: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
        for entry in calls:
            key = call_key(entry)
            if key in self._index:
                raise ValueError(f"call log holds {key} twice")
            self._index[key] = entry
        self._occurrence: Counter = Counter()
        self.served: List[Dict[str, Any]] = []
        self.mismatches: List[ReplayMismatch] = []

    def serve(self, role: str, canonical: List[Dict[str, Any]]) -> Dict[str, Any]:
        digest = request_sha256(canonical)
        with self._lock:
            occurrence = self._occurrence[(role, digest)]
            self._occurrence[(role, digest)] += 1
            entry = self._index.get((role, digest, occurrence))
            if entry is None:
                err = ReplayMismatch(self._explain(role, canonical, digest, occurrence))
                self.mismatches.append(err)
                raise err
            self.served.append(entry)
            return entry

    def unused(self) -> List[Dict[str, Any]]:
        with self._lock:
            used = {call_key(e) for e in self.served}
            return sorted((e for k, e in self._index.items() if k not in used),
                          key=lambda e: e["seq"])

    def check(self) -> None:
        if self.mismatches:
            raise self.mismatches[0]
        left = self.unused()
        if self.strict and left:
            listing = ", ".join(f"{e['role']}#{e['seq']} ({e['request_sha256'][:12]}"
                                f"/{e['occurrence']})" for e in left[:10])
            raise ReplayLeftover(
                f"replay finished with {len(left)} recorded call(s) never requested: "
                f"{listing}{' ...' if len(left) > 10 else ''}")

    def _explain(self, role: str, canonical, digest: str, occurrence: int) -> str:
        head = (f"replay mismatch: role {role!r} sent a request not in the call log "
                f"(request_sha256 {digest[:12]}..., occurrence {occurrence})")
        same = [e for (r, d, _), e in self._index.items() if r == role and d == digest]
        if same:
            return (f"{head}. This exact request was recorded {len(same)} time(s); the "
                    f"replay asked for it {occurrence + 1} times.")
        candidates = [e for (r, _, _), e in self._index.items() if r == role]
        if not candidates:
            return f"{head}. The log has no {role!r} calls at all."
        asked = render_request(canonical)
        best = max(candidates, key=lambda e: difflib.SequenceMatcher(
            None, render_request(e["request"]["messages"]), asked, autojunk=False).quick_ratio())
        recorded = render_request(best["request"]["messages"])
        diff = list(difflib.unified_diff(
            recorded.splitlines(), asked.splitlines(),
            fromfile=f"recorded {role}#{best['seq']}", tofile="replay request",
            lineterm="", n=1))
        shown = "\n".join(diff[:40]) + ("\n... (diff truncated)" if len(diff) > 40 else "")
        return f"{head}. Closest recorded {role!r} request, as a diff:\n{shown}"


class ReplayChat:
    """Answers chat calls for one role from a :class:`ReplayLog`."""

    def __init__(self, role: str, log: ReplayLog, spec: str = "ollama:replay"):
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; known: {ROLES}")
        self.role = role
        self.log = log
        self.spec = spec

    def __call__(self, spec: str, messages: Any, **params: Any) -> Any:
        from app.agents.llm_chat import Reply

        entry = self.log.serve(self.role, canonical_messages(messages))
        if entry["error"] is not None:
            err = entry["error"]
            raise ReplayedLLMError(f"{err['type']}: {err['message']}")
        response = entry["response"]
        usage = response.get("usage") or {}
        meta = response.get("response_metadata") or {}
        return Reply(text=response["content"] or "", input_tokens=usage.get("input_tokens"),
                     output_tokens=usage.get("output_tokens"),
                     finish_reason=meta.get("finish_reason"),
                     truncated_prompt=bool(meta.get("truncated_prompt")), model=spec)


# --------------------------------------------------------------------------
# Deterministic recipe ids
# --------------------------------------------------------------------------

#: Fixed namespace for :class:`ContentRecipeIds` (any constant would do).
RECIPE_ID_NAMESPACE = uuid.UUID("6f1c2d0e-4b1a-5e8f-9a3c-2e7d5b0c9f14")


class ContentRecipeIds:
    """``PlanDeps.recipe_ids`` for evaluation: uuid5 of (chef id, recipe, n).

    ``n`` counts earlier recipes with the same chef id and content in this
    run, so a regenerated recipe identical to its original still gets a new
    id. Keyed on content rather than call order, so the parallel SousChefs
    get the same ids however their threads interleave.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: Counter = Counter()

    def __call__(self, chef_id: str, recipe: Any) -> str:
        data = recipe.model_dump(mode="json") if hasattr(recipe, "model_dump") else recipe
        body = json.dumps({"chef_id": chef_id, "recipe": jsonable(data)},
                          sort_keys=True, ensure_ascii=False)
        with self._lock:
            n = self._seen[body]
            self._seen[body] += 1
        return str(uuid.uuid5(RECIPE_ID_NAMESPACE, f"{body}#{n}"))
