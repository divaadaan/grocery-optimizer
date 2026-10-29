import logging
from typing import Callable, Optional

from app.agents.deps import GraphDeps
from app.agents.graph import create_recipe_generation_graph
from app.agents.state import RecipeGenerationState
from app.services import products
from app.services.budget import DEFAULT_PERIOD

logger = logging.getLogger(__name__)

# MLflow is configured lazily by MLflowLogger on first use, so an unreachable
# tracking server can't break recipe generation.


def run_recipe_generation(
    user_id: int,
    postal_code: str,
    budget: float,
    household_size: int,
    dietary_restrictions: list,
    num_meals: int,
    preferences: dict = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    budget_period: str = DEFAULT_PERIOD,
    budget_spent_this_period: float = 0.0,
    graph_deps: Optional[GraphDeps] = None,
):
    """
    Main entry point for recipe generation workflow.

    `budget` is the user's limit for one budget period (a calendar week or
    month — `app/services/budget.py`), not a per-generation allowance.
    `budget_spent_this_period` is what their completed shopping lists have
    already consumed of it; the caller supplies it (the route reads it from
    `GET /users/{id}/budget`'s service) so this entry point stays free of
    database access. It defaults to 0.0, which reproduces the old
    "whole budget every time" behaviour — so callers must pass it, and the
    route does.

    `progress_callback`, if given, is called with the name of each graph node
    as it finishes — including once per parallel SousChef task — so a caller
    (the job registry) can report real progress stages. It must not raise;
    exceptions from it are logged and swallowed rather than failing a
    generation that is otherwise fine.

    `graph_deps` overrides the graph's external dependencies (LLMs, DB reads,
    the recipe store, MLflow — `app/agents/deps.py`); `None` is production.

    Raises `products.UnsupportedRestrictionError` before any work when a
    restriction label is not one the app enforces (e.g. a stored profile from
    before the API refused `halal`): generating "compliant" recipes for a
    restriction nothing checks would be a false promise.
    """
    products.require_supported_restrictions(dietary_restrictions)

    # Initialize state
    initial_state = RecipeGenerationState(
        user_id=user_id,
        postal_code=postal_code,
        budget=budget,
        budget_period=budget_period,
        budget_spent_this_period=budget_spent_this_period,
        household_size=household_size,
        dietary_restrictions=dietary_restrictions,
        num_meals=num_meals,
        preferences=preferences or {},
        # All other fields initialized by Chef
        available_deals=[],
        deal_index={},
        ingredient_groups=[],
        ingredient_reuse_map={},
        target_ingredients_per_group=0,
        generated_recipes={},
        sous_chef_assignments={},
        validation_results={},
        approved_recipe_ids=[],
        rejected_recipe_ids=[],
        iteration_count=0,
        max_iterations=2,
        retry_strategy="reassign_chef",
        recipes_pending_retry={},
        total_cost=0.0,
        cost_per_meal=0.0,
        estimated_savings=None,
        budget_remaining=budget - budget_spent_this_period,
        mlflow_run_id="",
        agent_call_log=[],
        status="initializing",
        errors=[],
        warnings=[]
    )

    # Create and run graph
    graph = create_recipe_generation_graph(graph_deps)

    print("=" * 60)
    print("STARTING RECIPE GENERATION WORKFLOW")
    print("=" * 60)

    # Streamed rather than invoked so node-completion events are observable.
    # "values" yields the full state after each superstep (the last one is
    # what invoke() would have returned); "updates" yields {node: writes}
    # per finished task, which is where progress comes from.
    final_state = None
    for mode, chunk in graph.stream(initial_state, stream_mode=["updates", "values"]):
        if mode == "values":
            final_state = chunk
        elif progress_callback is not None:
            for node_name in chunk:
                try:
                    progress_callback(node_name)
                except Exception as exc:
                    logger.warning("progress_callback failed for %s: %s", node_name, exc)

    if final_state is None:
        raise RuntimeError("Recipe generation graph produced no state")

    print("=" * 60)
    print("WORKFLOW COMPLETED")
    print(f"Status: {final_state['status']}")
    print(f"Recipes Generated: {len(final_state['generated_recipes'])}")
    print(f"Recipes Approved: {len(final_state['approved_recipe_ids'])}")
    print(f"Total Cost: ${final_state['total_cost']:.2f}")
    print(f"Iterations: {final_state['iteration_count']}")
    print("=" * 60)

    return final_state


# Example usage
if __name__ == "__main__":
    result = run_recipe_generation(
        user_id=1,
        postal_code="M5V3A8",  # Toronto
        budget=75.00,
        household_size=2,
        dietary_restrictions=["vegetarian", "no_nuts"],
        num_meals=7,
        preferences={
            "cuisine_preferences": ["Italian", "Asian"],
            "avoid_ingredients": ["mushrooms"]
        }
    )

    # Access results
    if result["status"] == "completed":
        print("\n✓ SUCCESS! Approved recipes:")
        for recipe_id in result["approved_recipe_ids"]:
            recipe = result["generated_recipes"][recipe_id]
            print(f"  - {recipe['name']} (${recipe['total_cost']:.2f})")
    else:
        print(f"\n✗ FAILED: {result['errors']}")
