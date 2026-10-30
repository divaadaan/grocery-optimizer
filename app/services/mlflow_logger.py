"""MLflow tracking for recipe generation: one MLflow run per generation.

Best-effort observability: configured lazily on first use, and a tracking
failure never takes down the recipe workflow.

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
"""
from __future__ import annotations

import contextlib
import functools
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

import mlflow

from app.config import settings

# Configured lazily on first use; see _ensure_configured.
_configured = False
_available = False
_client: Any = None
_experiment_id: Optional[str] = None
_config_lock = threading.Lock()

_TOKEN_KEYS = ("input_tokens", "output_tokens", "total_tokens")


@dataclass
class _RunScope:
    """The MLflow run of one generation, shared by all its graph tasks."""
    generation_id: Optional[str] = None
    run_id: Optional[str] = None


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


def _ensure_configured() -> bool:
    global _configured, _available, _client, _experiment_id
    with _config_lock:
        if not _configured:
            _configured = True
            try:
                mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
                experiment = mlflow.set_experiment(settings.mlflow_experiment_name)
                _experiment_id = getattr(experiment, "experiment_id", None)
                _client = mlflow.MlflowClient()
                _available = True
            except Exception as e:
                print(f"[MLflow] Tracking unavailable, continuing without it: {e}")
                _available = False
    return _available


def _best_effort(default=None):
    """Skip the call if MLflow is unreachable; swallow tracking errors."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not _ensure_configured():
                return default
            try:
                return func(*args, **kwargs)
            except Exception as e:
                print(f"[MLflow] {func.__name__} failed (ignored): {e}")
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
    def log_ingredient_groups(groups: List[List[Dict]], reuse_map: Dict[str, int]):
        """Log Chef's ingredient grouping strategy."""
        run_id = _current_run_id()
        if run_id is None:
            return
        _log_metrics(run_id, {
            "ingredient_groups_count": len(groups),
            "avg_ingredients_per_group": sum(len(g) for g in groups) / len(groups),
            "total_unique_ingredients": len(reuse_map),
            "max_ingredient_reuse": max(reuse_map.values()),
            "avg_ingredient_reuse": sum(reuse_map.values()) / len(reuse_map)
        })

        # Log reuse map as artifact
        _client.log_dict(run_id, reuse_map, "ingredient_reuse_map.json")

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
