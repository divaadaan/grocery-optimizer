"""MLflow tracking for recipe generation: one MLflow run per generation.

Best-effort observability: configured lazily on first use, and a tracking
failure never takes down -- or stalls -- the recipe workflow.

One run per generation, across threads
--------------------------------------
The pipeline writes each dish's recipe on a pool thread, and MLflow's fluent
API keeps the active run *per thread*, so logging through it from a worker
thread would silently start an orphan run. Nothing here uses the fluent
active run: every call goes through ``MlflowClient`` with an explicit
``run_id``, looked up from a :class:`_RunScope`:

* ``plan_pipeline.run_plan_generation`` opens :func:`generation_scope`. The
  scope lives in a ``ContextVar``, and the pipeline runs each worker in a copy
  of its context, so every stage of one generation, on any thread, sees the
  same scope object -- and concurrent generations each see their own.
* ``start_run`` creates the run and stores its id on that shared scope
  object; every later call logs to it.
* A call with no run to log to is skipped. It never creates one.

Tokens are logged only as the provider reported them; a count the provider
did not report is not logged.

What a run holds
----------------
Per-agent duration and token metrics and model tags (``log_agent_call``),
``dish_plan.json`` (the validated plan), final metrics, and in
``finalize_run`` the approved recipes and ``agent_call_log.json`` -- every
LLM call's full prompt and replies.

**Privacy:** ``agent_call_log.json`` holds full prompts -- the user's dietary
profile, preferences and the offered deals. Fine for the local file store;
think before pointing ``MLFLOW_TRACKING_URI`` at a shared server.

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
            # No generation_scope (a stage driven outside run_plan_generation):
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
    def log_dict(data: Any, artifact_file: str):
        """Log one JSON artifact (e.g. ``dish_plan.json``) into the run."""
        run_id = _current_run_id()
        if run_id is None:
            return
        _client.log_dict(run_id, data, artifact_file)

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
