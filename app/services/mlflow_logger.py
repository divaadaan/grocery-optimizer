"""MLflow tracking for recipe generation: one MLflow run (and one trace) per
generation.

Best-effort observability: configured lazily on first use, and a tracking
failure never takes down -- or stalls -- the recipe workflow.

One run per generation, across threads
--------------------------------------
LangGraph runs nodes on pool threads (the three SousChefs in parallel), and
MLflow's fluent API keeps the active run *per thread*. Logging through it from
a thread that never called ``mlflow.start_run`` silently started a fresh run:
every SousChef call became an orphan run. So nothing here uses the fluent
active run. Every call goes through ``MlflowClient`` with an explicit
``run_id``, looked up from a :class:`_RunScope`:

* ``run_recipe_generation`` opens :func:`generation_scope` around the graph.
  The scope lives in a ``ContextVar``, and LangGraph copies the caller's
  context into every task it runs, so all nodes of one generation, on any
  thread, see the same scope object -- and concurrent generations each see
  their own.
* ``start_run`` (the Chef's first node) creates the run and stores its id on
  that shared scope object; every later call logs to it.
* A call with no run to log to is skipped. It never creates one.

Tokens are logged only as the provider reported them (``usage_metadata``,
via ``app.agents.llm_usage``); a count the provider did not report is not
logged. The old ``*_tokens`` metric was ``len(raw)`` -- characters -- and is
gone.

Tracing (one trace per generation)
----------------------------------
:func:`generation_trace` wraps the graph run in a root span
(``recipe_generation``) and hands back an ``MlflowLangchainTracer`` to pass as
the graph's ``callbacks``. LangChain propagates callbacks through the config
LangGraph gives every task -- including the ``Send`` fan-out's SousChef
threads -- and the tracer parents each span by the OTel context the task
copied from its caller (or the LangChain ``parent_run_id``), never by thread.
So every node, and inside it every chat-model call (full prompt, raw
response, provider usage, latency), lands in the *one* trace. It is an
explicit callback rather than ``mlflow.langchain.autolog()``: autolog patches
LangChain process-wide, which would also trace eval replays and tests that
never asked for it.

The trace is linked to the generation's run (``mlflow.sourceRun`` metadata,
which is what the run page's Traces tab searches by) and tagged with
``generation_id``; the run gets a ``trace_request_id`` tag back. The run is
created inside the graph (``start_run``), after the trace has started, so the
link is written just before the root span closes.

**Privacy:** traces (and ``chef_plan.json``) hold full prompts -- the user's
dietary profile, budget, preferences and the served deal lists. Fine for the
local file store; think before pointing ``MLFLOW_TRACKING_URI`` at a shared
server (or set ``MLFLOW_TRACING=false``).

Outages never block generation
------------------------------
A dead ``http://`` tracking server used to stall each generation 15-20
minutes in MLflow's own retries (7 retries, 120 s timeouts). Now:

* :func:`_ensure_configured` probes an http(s) tracking URI once (``GET
  /health``, :data:`PROBE_TIMEOUT_S` connect/read). Unreachable -> a warning
  and the fallback store: ``MLFLOW_FALLBACK_TRACKING_URI``, by default the
  repo's gitignored ``./mlruns`` (view it with ``mlflow ui
  --backend-store-uri ./mlruns``). Falling back rather than disabling keeps
  the run inspectable -- the reason tracking exists -- at no cost;
  ``MLFLOW_FALLBACK_TRACKING_URI=off`` disables tracking instead.
* MLflow's HTTP retry/timeout knobs are capped (:data:`HTTP_ENV_CAPS`, via
  ``os.environ.setdefault`` so an explicit setting wins), so a server that
  dies mid-run costs one bounded request. A connection failure then trips a
  breaker: the rest of that generation skips tracking, and the next
  generation re-probes (and falls back if the server is still gone).
"""
from __future__ import annotations

import contextlib
import functools
import logging
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlparse

import mlflow

from app.config import settings

logger = logging.getLogger(__name__)

# Configured lazily on first use; see _ensure_configured.
_configured = False
_available = False
_client: Any = None
_experiment_id: Optional[str] = None
_config_lock = threading.Lock()

_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")

# The gitignored local file store tracking falls back to.
DEFAULT_FALLBACK_URI = (Path(__file__).resolve().parents[2] / "mlruns").as_uri()

# Probe of an http(s) tracking server: connect and read timeout, seconds.
PROBE_TIMEOUT_S = 2.0

# MLflow 2.22's defaults are 7 retries, backoff factor 2 and a 120 s timeout
# per request (mlflow/environment_variables.py; read per request by
# mlflow/utils/rest_utils.py). Capped so one request against a server that
# dies mid-run is bounded (2 attempts x 5 s + ~1 s backoff); explicit env wins.
HTTP_ENV_CAPS = {
    "MLFLOW_HTTP_REQUEST_MAX_RETRIES": "1",
    "MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR": "1",
    "MLFLOW_HTTP_REQUEST_TIMEOUT": "5",
}

_DISABLED_WORDS = ("off", "none", "disabled", "false")


@dataclass
class _RunScope:
    """The MLflow run of one generation, shared by all its graph tasks."""
    generation_id: Optional[str] = None
    run_id: Optional[str] = None
    # Set when the tracking server stopped answering mid-generation: the rest
    # of this generation skips MLflow.
    tripped: bool = False


_scope: ContextVar[Optional[_RunScope]] = ContextVar("mlflow_generation_scope", default=None)


@contextlib.contextmanager
def generation_scope(generation_id: Optional[str]) -> Iterator[_RunScope]:
    """Bind one generation's MLflow run to this context (and every graph task
    started from it). Pure bookkeeping: touches no MLflow API, so it is safe
    under the eval harness's offline guard."""
    scope = _RunScope(generation_id=generation_id)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


# --------------------------------------------------------------------------
# Configuration: probe, fallback, capped retries
# --------------------------------------------------------------------------

def _cap_http_retries() -> None:
    for key, value in HTTP_ENV_CAPS.items():
        os.environ.setdefault(key, value)


def _probe(uri: str, timeout: float = PROBE_TIMEOUT_S) -> Optional[str]:
    """``None`` if ``uri`` can be used, else why not. Only http(s) URIs are
    probed (one GET, no retries); anything else -- ``file:``, a plain path,
    databricks -- is MLflow's to resolve."""
    if urlparse(uri).scheme not in ("http", "https"):
        return None
    import requests  # noqa: PLC0415 -- an MLflow dependency

    try:
        requests.get(uri.rstrip("/") + "/health", timeout=(timeout, timeout))
    except requests.RequestException as exc:
        return f"{type(exc).__name__}: {exc}"
    return None  # any HTTP answer means a server is there


def _fallback_uri() -> Optional[str]:
    configured = (settings.mlflow_fallback_tracking_uri or "").strip()
    if configured.lower() in _DISABLED_WORDS:
        return None
    return configured or DEFAULT_FALLBACK_URI


def _resolve_tracking_uri() -> Optional[str]:
    """The URI to track to: the configured one if it answers, else the
    fallback (``None`` = tracking disabled)."""
    uri = settings.mlflow_tracking_uri
    problem = _probe(uri)
    if problem is None:
        return uri
    fallback = _fallback_uri()
    outcome = f"tracking to {fallback} instead" if fallback else "tracking disabled"
    logger.warning("MLflow tracking server %s unreachable (%s); %s", uri, problem, outcome)
    print(f"[MLflow] Tracking server {uri} unreachable ({problem}); {outcome}")
    return fallback


def _ensure_configured() -> bool:
    global _configured, _available, _client, _experiment_id
    with _config_lock:
        if not _configured:
            _configured = True
            try:
                _cap_http_retries()
                uri = _resolve_tracking_uri()
                if uri is None:
                    _available = False
                else:
                    mlflow.set_tracking_uri(uri)
                    experiment = mlflow.set_experiment(settings.mlflow_experiment_name)
                    _experiment_id = getattr(experiment, "experiment_id", None)
                    _client = mlflow.MlflowClient()
                    _available = True
            except Exception as e:
                print(f"[MLflow] Tracking unavailable, continuing without it: {e}")
                _available = False
    return _available


def _reset_configuration() -> None:
    """Forget the configuration so the next call re-probes (breaker, tests)."""
    global _configured, _available, _client, _experiment_id
    with _config_lock:
        _configured = False
        _available = False
        _client = None
        _experiment_id = None


def _is_connection_failure(exc: Optional[BaseException]) -> bool:
    """A tracking server that stopped answering, vs. any other error."""
    import requests  # noqa: PLC0415

    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (requests.ConnectionError, requests.Timeout,
                            ConnectionError, TimeoutError)):
            return True
        text = str(exc)
        if "Max retries exceeded" in text or "timeout exception" in text:
            return True  # MlflowException wrapping the requests error by text
        exc = exc.__cause__ or exc.__context__
    return False


def _trip(reason: str) -> None:
    """Stop tracking for the rest of this generation; re-probe next time."""
    scope = _scope.get()
    if scope is not None:
        scope.tripped = True
    _reset_configuration()
    print(f"[MLflow] Tracking server stopped answering ({reason}); "
          "skipping tracking for the rest of this generation")


def _best_effort(default=None):
    """Skip the call if MLflow is unreachable; swallow tracking errors."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            scope = _scope.get()
            if scope is not None and scope.tripped:
                return default
            if not _ensure_configured():
                return default
            try:
                return func(*args, **kwargs)
            except Exception as e:
                print(f"[MLflow] {func.__name__} failed (ignored): {e}")
                if _is_connection_failure(e):
                    _trip(type(e).__name__)
                return default
        return wrapper
    return decorator


def _current_run_id() -> Optional[str]:
    scope = _scope.get()
    return scope.run_id if scope is not None else None


def _log_metrics(run_id: str, metrics: Dict[str, float]) -> None:
    now_ms = int(time.time() * 1000)
    for key, value in metrics.items():
        _client.log_metric(run_id, key, value, timestamp=now_ms)


def _token_metrics(prefix: str, usage: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Provider-reported counts only: non-negative ints, never bools/None."""
    out: Dict[str, int] = {}
    for key in _TOKEN_KEYS:
        value = (usage or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[f"{prefix}_{key}"] = value
    return out


class MLflowLogger:
    """Track all agent interactions and metrics (one run per generation)."""

    @staticmethod
    @_best_effort(default="")
    def start_run(user_id: int, num_meals: int, budget: float,
                  dietary_restrictions: list, generation_id: Optional[str] = None,
                  models: Optional[Dict[str, Dict[str, str]]] = None) -> str:
        """Create the generation's run and bind it to the generation scope.

        ``models`` is ``role -> {provider, model}``; each role's model and
        provider are logged as params.
        """
        scope = _scope.get()
        if scope is None:
            # No generation_scope (a node driven outside run_recipe_generation):
            # bind one here. It reaches only this context, never other threads.
            scope = _RunScope(generation_id=generation_id)
            _scope.set(scope)
        if generation_id is None:
            generation_id = scope.generation_id

        tags = {"generation_id": generation_id} if generation_id else {}
        run = _client.create_run(
            _experiment_id,
            run_name=f"recipe_gen_user_{user_id}_{int(time.time())}",
            tags=tags,
        )
        run_id = run.info.run_id
        scope.run_id = run_id

        params: Dict[str, Any] = {
            "user_id": user_id,
            "num_meals": num_meals,
            "budget": budget,
            "dietary_restrictions": ",".join(dietary_restrictions),
            "timestamp": time.time(),
        }
        if generation_id:
            params["generation_id"] = generation_id
        for role, spec in sorted((models or {}).items()):
            params[f"model_{role}"] = (spec or {}).get("model")
            params[f"provider_{role}"] = (spec or {}).get("provider")
        for key, value in params.items():
            _client.log_param(run_id, key, value)

        return run_id

    @staticmethod
    @_best_effort()
    def log_agent_call(agent_name: str, duration: float, model: str, success: bool,
                       error: str = None, usage: Optional[Dict[str, Any]] = None,
                       provider: Optional[str] = None):
        """Log one agent invocation into the generation's run.

        ``usage`` is the provider-reported token usage
        (``llm_usage.MeteredLLM.usage``); only the counts it carries are
        logged, as ``<agent>_input_tokens`` / ``_output_tokens`` /
        ``_total_tokens``. Absent usage logs no token metric at all.
        """
        run_id = _current_run_id()
        if run_id is None:
            return
        prefix = agent_name.lower().replace(" ", "_")

        _log_metrics(run_id, {f"{prefix}_duration_sec": duration,
                              **_token_metrics(prefix, usage)})
        # Tags, not params: a tag may be re-set (retries, repeated calls).
        _client.set_tag(run_id, f"{prefix}_model", model)
        if provider:
            _client.set_tag(run_id, f"{prefix}_provider", provider)
        _client.set_tag(run_id, f"{prefix}_success", str(bool(success)))
        if error:
            _client.set_tag(run_id, f"{prefix}_error", error[:200])  # Truncate

    @staticmethod
    @_best_effort()
    def log_ingredient_groups(groups: List[List[Dict]], reuse_map: Dict[str, int],
                              plan: Optional[Dict[str, Any]] = None):
        """Log the Chef's ingredient groups and, if given, its whole plan.

        ``plan`` (built by ``ChefOrchestrator.plan_ingredient_groups``) is
        logged as ``chef_plan.json``: the model's raw selections, what
        ``_restrict_to_served_deals`` dropped and why, what it backfilled
        into which group, the final (merged) groups, and how many served
        deals the prompt showed vs. were available.

        Models routinely return an empty ``ingredient_reuse_map`` (the
        schema defaults it to ``{}``). This used to evaluate
        ``max({}.values())``, raise ``ValueError`` before logging anything,
        and -- swallowed by ``_best_effort`` -- record nothing at all, groups
        included. Reuse metrics are now logged only when there is a map to
        summarise.
        """
        run_id = _current_run_id()
        if run_id is None:
            return
        reuse_map = reuse_map or {}
        metrics: Dict[str, float] = {"ingredient_groups_count": len(groups)}
        if groups:
            metrics["avg_ingredients_per_group"] = sum(len(g) for g in groups) / len(groups)
        metrics["total_unique_ingredients"] = len(reuse_map)
        if reuse_map:
            metrics["max_ingredient_reuse"] = max(reuse_map.values())
            metrics["avg_ingredient_reuse"] = sum(reuse_map.values()) / len(reuse_map)
        if plan:
            metrics["chef_selections_dropped"] = len(plan.get("dropped") or [])
            metrics["chef_selections_backfilled"] = len(plan.get("backfilled") or [])
            for key in ("served_deals_available", "served_deals_in_prompt"):
                if isinstance(plan.get(key), int):
                    metrics[f"chef_{key}"] = plan[key]
        _log_metrics(run_id, metrics)

        _client.log_dict(run_id, reuse_map, "ingredient_reuse_map.json")
        _client.log_dict(run_id, plan if plan else {"final_groups": groups},
                         "chef_plan.json")

    @staticmethod
    @_best_effort()
    def log_validation_results(validation_results: Dict[str, Any]):
        """Log Nutritionist validation metrics."""
        run_id = _current_run_id()
        if run_id is None:
            return
        approved = sum(1 for r in validation_results.values() if r["approved"])
        rejected = len(validation_results) - approved

        avg_health_score = (
            sum(r["health_score"] for r in validation_results.values())
            / len(validation_results)
        ) if validation_results else 0

        _log_metrics(run_id, {
            "recipes_validated": len(validation_results),
            "recipes_approved": approved,
            "recipes_rejected": rejected,
            "approval_rate": approved / len(validation_results) if validation_results else 0,
            "avg_health_score": avg_health_score
        })

        # Log rejection reasons
        rejection_reasons = [
            r["feedback"] for r in validation_results.values() if not r["approved"]
        ]
        if rejection_reasons:
            _client.log_text(run_id, "\n---\n".join(rejection_reasons),
                             "rejection_reasons.txt")

    @staticmethod
    @_best_effort()
    def log_final_metrics(total_cost: float, cost_per_meal: float,
                          estimated_savings: Optional[float], iterations: int,
                          recipe_count: int, success: bool):
        """Log final workflow metrics. Unknown savings (None) is not logged."""
        run_id = _current_run_id()
        if run_id is None:
            return
        metrics = {
            "total_cost": total_cost,
            "cost_per_meal": cost_per_meal,
            "iterations": iterations,
            "final_recipe_count": recipe_count
        }
        if estimated_savings is not None:
            metrics["estimated_savings"] = estimated_savings
        _log_metrics(run_id, metrics)

        _client.log_param(run_id, "workflow_success", success)

    @staticmethod
    @_best_effort()
    def finalize_run(state: Dict[str, Any]):
        """Log artifacts and close the generation's run."""
        run_id = _current_run_id()
        if run_id is None:
            return
        # Log approved recipes
        if state.get("approved_recipe_ids"):
            approved_recipes = {
                rid: state["generated_recipes"][rid]
                for rid in state["approved_recipe_ids"]
            }
            _client.log_dict(run_id, approved_recipes, "approved_recipes.json")

        # Log agent call timeline
        if state.get("agent_call_log"):
            _client.log_dict(run_id, state["agent_call_log"], "agent_call_log.json")

        status = "FINISHED" if state.get("status") == "completed" else "FAILED"
        _client.set_terminated(run_id, status=status)


# --------------------------------------------------------------------------
# Tracing
# --------------------------------------------------------------------------

def _make_tracer() -> Any:
    """MLflow's LangChain tracer, plus provider token usage on each LLM span.

    The stock tracer serializes the ``LLMResult`` without the message's
    ``usage_metadata``, so the counts are copied onto the span as a
    ``token_usage`` attribute -- provider-reported only, like the run's
    ``*_tokens`` metrics (absent when the provider reported none).
    """
    from mlflow.langchain.langchain_tracer import MlflowLangchainTracer  # noqa: PLC0415

    from app.agents.llm_usage import response_usage  # noqa: PLC0415

    class _UsageTracer(MlflowLangchainTracer):
        def on_llm_end(self, response, *, run_id, **kwargs):
            try:
                usage = _generation_usage(response, response_usage)
                if usage:
                    self._get_span_by_run_id(run_id).set_attribute("token_usage", usage)
            except Exception as e:  # observability only
                print(f"[MLflow] recording token usage on a span failed (ignored): {e}")
            return super().on_llm_end(response, run_id=run_id, **kwargs)

    return _UsageTracer()


def _generation_usage(response: Any, response_usage: Any) -> Dict[str, int]:
    """Summed provider counts over the result's messages; a key only when
    every message reported it (``llm_usage.MeteredLLM``'s rule)."""
    per_message = [response_usage(getattr(gen, "message", None))
                   for gens in (getattr(response, "generations", None) or [])
                   for gen in gens]
    out: Dict[str, int] = {}
    for key in _TOKEN_KEYS:
        values = [u[key] for u in per_message]
        if values and all(v is not None for v in values):
            out[key] = sum(values)
    return out


def _link_trace(request_id: str, scope: _RunScope) -> None:
    """Tag the still-open trace with the generation, point it at the run,
    and point the run back at it."""
    if scope.generation_id:
        mlflow.update_current_trace(tags={"generation_id": scope.generation_id})
    if not scope.run_id or scope.tripped or not _available:
        return
    from mlflow.tracing.constant import TraceMetadataKey  # noqa: PLC0415
    from mlflow.tracing.trace_manager import InMemoryTraceManager  # noqa: PLC0415

    # Metadata set before the root span ends is merged into the stored
    # TraceInfo at end_trace; the run page's Traces tab filters on it.
    with InMemoryTraceManager.get_instance().get_trace(request_id) as trace:
        if trace is not None:
            trace.info.request_metadata[TraceMetadataKey.SOURCE_RUN] = scope.run_id
    _client.set_tag(scope.run_id, "trace_request_id", request_id)


@contextlib.contextmanager
def generation_trace(scope: _RunScope, enabled: bool = True) -> Iterator[List[Any]]:
    """Trace one generation. Yields the LangChain callbacks to run the graph
    with: ``[]`` -- and nothing else happens -- when ``enabled`` is false,
    ``MLFLOW_TRACING`` is off or MLflow is unavailable. Otherwise opens the
    ``recipe_generation`` root span, so every graph span joins one trace,
    and links that trace to the generation's run before closing it.

    Never raises for a tracking reason; an exception from the graph itself
    propagates unchanged (the trace is then recorded with status ERROR).
    """
    span_cm = span = None
    callbacks: List[Any] = []
    if enabled and settings.mlflow_tracing and not scope.tripped:
        try:
            if _ensure_configured():
                span_cm = mlflow.start_span(name="recipe_generation", span_type="CHAIN")
                span = span_cm.__enter__()
                if scope.generation_id:
                    span.set_attribute("generation_id", scope.generation_id)
                callbacks = [_make_tracer()]
        except Exception as e:
            print(f"[MLflow] tracing unavailable (ignored): {e}")
            if span is not None:
                with contextlib.suppress(Exception):
                    span_cm.__exit__(None, None, None)
            span_cm = span = None
            callbacks = []

    if span_cm is None:
        yield callbacks
        return

    exc_info: tuple = (None, None, None)
    try:
        yield callbacks
    except BaseException as e:
        exc_info = (type(e), e, e.__traceback__)
        raise
    finally:
        try:
            _link_trace(span.request_id, scope)
        except Exception as e:
            print(f"[MLflow] linking trace to run failed (ignored): {e}")
        try:
            span_cm.__exit__(*exc_info)
        except Exception as e:
            print(f"[MLflow] closing trace failed (ignored): {e}")
