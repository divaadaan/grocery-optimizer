import functools
import os
import time
from typing import Dict, Any, List

# MLflow's default HTTP retry policy (7 retries, exponential backoff) stalls
# callers for ~4 minutes when the tracking server is down. Tracking is
# best-effort here, so fail fast instead; the env vars still win if set.
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "1")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "5")

import mlflow

from app.config import settings

# MLflow is best-effort observability: configure it lazily on first use and
# never let a tracking failure take down the recipe workflow.
_configured = False
_available = False


def _ensure_configured() -> bool:
    global _configured, _available
    if not _configured:
        _configured = True
        try:
            mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
            mlflow.set_experiment(settings.mlflow_experiment_name)
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


class MLflowLogger:
    """Track all agent interactions and metrics."""

    @staticmethod
    @_best_effort(default="")
    def start_run(user_id: int, num_meals: int, budget: float,
                  dietary_restrictions: list) -> str:
        """Initialize MLflow run."""
        mlflow.start_run(run_name=f"recipe_gen_user_{user_id}_{int(time.time())}")

        # Log input parameters
        mlflow.log_params({
            "user_id": user_id,
            "num_meals": num_meals,
            "budget": budget,
            "dietary_restrictions": ",".join(dietary_restrictions),
            "timestamp": time.time()
        })

        return mlflow.active_run().info.run_id

    @staticmethod
    @_best_effort()
    def log_agent_call(agent_name: str, tokens: int, duration: float,
                       model: str, success: bool, error: str = None):
        """Log individual agent invocation."""
        prefix = agent_name.lower().replace(" ", "_")

        mlflow.log_metrics({
            f"{prefix}_tokens": tokens,
            f"{prefix}_duration_sec": duration,
        }, step=int(time.time()))

        if error:
            mlflow.log_param(f"{prefix}_error", error[:200])  # Truncate

    @staticmethod
    @_best_effort()
    def log_ingredient_groups(groups: List[List[Dict]], reuse_map: Dict[str, int]):
        """Log Chef's ingredient grouping strategy."""
        mlflow.log_metrics({
            "ingredient_groups_count": len(groups),
            "avg_ingredients_per_group": sum(len(g) for g in groups) / len(groups),
            "total_unique_ingredients": len(reuse_map),
            "max_ingredient_reuse": max(reuse_map.values()),
            "avg_ingredient_reuse": sum(reuse_map.values()) / len(reuse_map)
        })

        # Log reuse map as artifact
        mlflow.log_dict(reuse_map, "ingredient_reuse_map.json")

    @staticmethod
    @_best_effort()
    def log_validation_results(validation_results: Dict[str, Any]):
        """Log Nutritionist validation metrics."""
        approved = sum(1 for r in validation_results.values() if r["approved"])
        rejected = len(validation_results) - approved

        avg_health_score = (
            sum(r["health_score"] for r in validation_results.values())
            / len(validation_results)
        ) if validation_results else 0

        mlflow.log_metrics({
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
            mlflow.log_text("\n---\n".join(rejection_reasons), "rejection_reasons.txt")

    @staticmethod
    @_best_effort()
    def log_final_metrics(total_cost: float, cost_per_meal: float,
                          estimated_savings: float, iterations: int,
                          recipe_count: int, success: bool):
        """Log final workflow metrics."""
        mlflow.log_metrics({
            "total_cost": total_cost,
            "cost_per_meal": cost_per_meal,
            "estimated_savings": estimated_savings,
            "iterations": iterations,
            "final_recipe_count": recipe_count
        })

        mlflow.log_param("workflow_success", success)

    @staticmethod
    @_best_effort()
    def finalize_run(state: Dict[str, Any]):
        """Log artifacts and close run."""
        # Log approved recipes
        if state.get("approved_recipe_ids"):
            approved_recipes = {
                rid: state["generated_recipes"][rid]
                for rid in state["approved_recipe_ids"]
            }
            mlflow.log_dict(approved_recipes, "approved_recipes.json")

        # Log agent call timeline
        if state.get("agent_call_log"):
            mlflow.log_dict(state["agent_call_log"], "agent_call_log.json")

        mlflow.end_run()
