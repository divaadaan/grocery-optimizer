from langgraph.graph import StateGraph, END
from langgraph.types import Send
from typing import Literal, List
from .state import RecipeGenerationState
from .chef_orchestrator import ChefOrchestrator
from .sous_chef import SousChef, sous_chef_generate_node
from .nutritionist import Nutritionist
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger

# Initialize agents
chef = ChefOrchestrator()
nutritionist = Nutritionist()
db_service = DatabaseService()


def generate_recipes_parallel(state: RecipeGenerationState):
    """
    Node 3: Fan-out to 3 parallel SousChef nodes.
    """
    print("[Orchestrator] Distributing work to 3 SousChefs...")

    # Calculate recipes per chef
    num_meals = state["num_meals"]
    recipes_per_chef = [
        (num_meals // 3) + (1 if i < num_meals % 3 else 0)
        for i in range(3)
    ]

    # Create Send objects for parallel execution
    return [
        Send("sous_chef_generate", {
            **state,
            "chef_id": f"sous_chef_{i+1}",
            "ingredient_group": state["ingredient_groups"][i],
            "target_recipe_count": recipes_per_chef[i]
        })
        for i in range(3)
    ]


def aggregate_recipes(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 5: Aggregate results from parallel SousChefs.
    """
    print(f"[Orchestrator] Aggregating {len(state['generated_recipes'])} recipes...")

    # Calculate total cost
    total_cost = sum(
        recipe["total_cost"]
        for recipe in state["generated_recipes"].values()
    )

    state["total_cost"] = total_cost
    state["cost_per_meal"] = total_cost / len(state["generated_recipes"]) if state["generated_recipes"] else 0
    state["budget_remaining"] = state["budget"] - total_cost

    # Calculate estimated savings (compare to regular prices)
    # This would use deal_index to compare sale_price vs regular_price
    state["estimated_savings"] = 0.0  # Placeholder

    print(f"[Orchestrator] Total cost: ${total_cost:.2f}, Budget remaining: ${state['budget_remaining']:.2f}")

    return state


def route_after_validation(state: RecipeGenerationState) -> Literal["finalize", "retry", "finalize_partial", "handle_failure"]:
    """
    Conditional edge: Decide next step after Nutritionist validation.
    """
    num_rejected = len(state["rejected_recipe_ids"])
    num_approved = len(state["approved_recipe_ids"])
    num_requested = state["num_meals"]

    print(f"[Router] Approved: {num_approved}, Rejected: {num_rejected}, Iteration: {state['iteration_count']}")

    # All approved - success!
    if num_rejected == 0:
        return "finalize"

    # Max iterations exceeded
    if state["iteration_count"] >= state["max_iterations"]:
        # Check for partial success (60% threshold)
        if num_approved >= num_requested * 0.6:
            print("[Router] Partial success - finalizing with approved recipes")
            state["warnings"].append(f"Only {num_approved}/{num_requested} recipes approved")
            return "finalize_partial"
        else:
            print("[Router] Insufficient approved recipes - failing")
            return "handle_failure"

    # Retry available
    print("[Router] Retrying rejected recipes")
    return "retry"


def retry_generation(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 8: Regenerate rejected recipes with feedback.
    """
    print(f"[Orchestrator] Retrying {len(state['recipes_pending_retry'])} recipes...")

    sous_chef = SousChef()

    for recipe_id, feedback in state["recipes_pending_retry"].items():
        original_recipe = state["generated_recipes"][recipe_id]

        # Find original ingredient group
        original_chef_id = state["sous_chef_assignments"][recipe_id]
        chef_index = int(original_chef_id.split("_")[-1]) - 1
        ingredient_group = state["ingredient_groups"][chef_index]

        # Regenerate
        new_recipe = sous_chef.regenerate_with_feedback(
            chef_id=original_chef_id,
            original_recipe=original_recipe,
            feedback=feedback,
            ingredient_group=ingredient_group,
            household_size=state["household_size"],
            dietary_restrictions=state["dietary_restrictions"]
        )

        if new_recipe:
            # Replace old recipe
            state["generated_recipes"][new_recipe["recipe_id"]] = new_recipe
            state["sous_chef_assignments"][new_recipe["recipe_id"]] = original_chef_id

            # Remove old recipe from rejected list
            if recipe_id in state["rejected_recipe_ids"]:
                state["rejected_recipe_ids"].remove(recipe_id)

    # Clear pending retries
    state["recipes_pending_retry"] = {}

    return state


def finalize_meal_plan(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 9: Finalize and save approved recipes.
    """
    print("[Orchestrator] Finalizing meal plan...")

    approved_recipes = [
        state["generated_recipes"][rid]
        for rid in state["approved_recipe_ids"]
    ]

    # Save to database
    recipe_ids = db_service.save_recipes(state["user_id"], approved_recipes)

    print(f"[Orchestrator] Saved {len(recipe_ids)} recipes to database")

    # Log final metrics to MLflow
    MLflowLogger.log_final_metrics(
        total_cost=state["total_cost"],
        cost_per_meal=state["cost_per_meal"],
        estimated_savings=state["estimated_savings"],
        iterations=state["iteration_count"],
        recipe_count=len(state["approved_recipe_ids"]),
        success=True
    )

    state["status"] = "completed"

    # Finalize MLflow run
    MLflowLogger.finalize_run(state)

    print(f"[Orchestrator] ✓ Meal plan complete! {len(approved_recipes)} recipes ready.")

    return state


def handle_failure(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 10: Handle workflow failure gracefully.
    """
    print("[Orchestrator] ✗ Workflow failed - graceful degradation")

    state["status"] = "failed"
    state["errors"].append("Unable to generate sufficient approved recipes")

    # Log failure to MLflow
    MLflowLogger.log_final_metrics(
        total_cost=state["total_cost"],
        cost_per_meal=state["cost_per_meal"],
        estimated_savings=state["estimated_savings"],
        iterations=state["iteration_count"],
        recipe_count=len(state["approved_recipe_ids"]),
        success=False
    )

    MLflowLogger.finalize_run(state)

    return state


# Build the graph
def create_recipe_generation_graph():
    """
    Construct the complete LangGraph workflow.
    """
    workflow = StateGraph(RecipeGenerationState)

    # Add nodes
    workflow.add_node("initialize_chef", chef.initialize)
    workflow.add_node("plan_ingredient_groups", chef.plan_ingredient_groups)
    workflow.add_node("generate_recipes_parallel", generate_recipes_parallel)
    workflow.add_node("sous_chef_generate", sous_chef_generate_node)
    workflow.add_node("aggregate_recipes", aggregate_recipes)
    workflow.add_node("nutritionist_validate", nutritionist.validate_recipes)
    workflow.add_node("handle_rejections", chef.handle_rejections)
    workflow.add_node("retry_generation", retry_generation)
    workflow.add_node("finalize_meal_plan", finalize_meal_plan)
    workflow.add_node("handle_failure", handle_failure)

    # Define edges
    workflow.set_entry_point("initialize_chef")
    workflow.add_edge("initialize_chef", "plan_ingredient_groups")
    workflow.add_edge("plan_ingredient_groups", "generate_recipes_parallel")
    workflow.add_edge("generate_recipes_parallel", "aggregate_recipes")
    workflow.add_edge("aggregate_recipes", "nutritionist_validate")

    # Conditional routing after validation
    workflow.add_conditional_edges(
        "nutritionist_validate",
        route_after_validation,
        {
            "finalize": "finalize_meal_plan",
            "retry": "handle_rejections",
            "finalize_partial": "finalize_meal_plan",
            "handle_failure": "handle_failure"
        }
    )

    # Retry loop
    workflow.add_edge("handle_rejections", "retry_generation")
    workflow.add_edge("retry_generation", "nutritionist_validate")

    # Terminal nodes
    workflow.add_edge("finalize_meal_plan", END)
    workflow.add_edge("handle_failure", END)

    return workflow.compile()
