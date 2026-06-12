from app.agents.graph import create_recipe_generation_graph
from app.agents.state import RecipeGenerationState

# MLflow is configured lazily by MLflowLogger on first use, so an unreachable
# tracking server can't break recipe generation.

def run_recipe_generation(
    user_id: int,
    postal_code: str,
    budget: float,
    household_size: int,
    dietary_restrictions: list,
    num_meals: int,
    preferences: dict = None
):
    """
    Main entry point for recipe generation workflow.
    """
    # Initialize state
    initial_state = RecipeGenerationState(
        user_id=user_id,
        postal_code=postal_code,
        budget=budget,
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
        estimated_savings=0.0,
        budget_remaining=budget,
        mlflow_run_id="",
        agent_call_log=[],
        status="initializing",
        errors=[],
        warnings=[]
    )

    # Create and run graph
    graph = create_recipe_generation_graph()

    print("=" * 60)
    print("STARTING RECIPE GENERATION WORKFLOW")
    print("=" * 60)

    final_state = graph.invoke(initial_state)

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
