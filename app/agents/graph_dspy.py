"""
DSPy 3.0 + LangGraph Recipe Generation Workflow

This module implements the complete multi-agent recipe generation system using:
- DSPy 3.0 for declarative LM programming
- LangGraph for stateful workflow orchestration

The workflow follows the same state graph as the original but with DSPy-powered agents:

Flow:
    initialize_chef_dspy
        ↓
    plan_ingredient_groups_dspy (DSPy ChainOfThought)
        ↓
    generate_recipes_parallel_dspy (fan-out to 3 SousChefs)
        ↓
    aggregate_recipes
        ↓
    validate_recipes_dspy (DSPy ChainOfThought)
        ↓
    [CONDITIONAL ROUTING]
    ├─→ finalize (100% approval)
    ├─→ retry (with DSPy Refine for regeneration)
    ├─→ finalize_partial (60% threshold)
    └─→ handle_failure (insufficient recipes)
"""

from langgraph.graph import StateGraph, END
from typing import Literal

from .state import RecipeGenerationState
from .dspy_langgraph_integration import (
    initialize_chef_dspy,
    plan_ingredient_groups_dspy,
    generate_recipes_parallel_dspy,
    sous_chef_generate_dspy,
    validate_recipes_dspy,
    handle_rejections_dspy,
    retry_generation_dspy,
)
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger


# Initialize database service
db_service = DatabaseService()


# ============================================================================
# UTILITY NODES (No LLM required)
# ============================================================================

def aggregate_recipes(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 5: Aggregate results from parallel SousChefs.
    """
    print(f"[Orchestrator DSPy] Aggregating {len(state['generated_recipes'])} recipes...")

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

    print(f"[Orchestrator DSPy] Total cost: ${total_cost:.2f}, Budget remaining: ${state['budget_remaining']:.2f}")

    return state


def route_after_validation(state: RecipeGenerationState) -> Literal["finalize", "retry", "finalize_partial", "handle_failure"]:
    """
    Conditional edge: Decide next step after Nutritionist validation.
    """
    num_rejected = len(state["rejected_recipe_ids"])
    num_approved = len(state["approved_recipe_ids"])
    num_requested = state["num_meals"]

    print(f"[Router DSPy] Approved: {num_approved}, Rejected: {num_rejected}, Iteration: {state['iteration_count']}")

    # All approved - success!
    if num_rejected == 0:
        return "finalize"

    # Max iterations exceeded
    if state["iteration_count"] >= state["max_iterations"]:
        # Check for partial success (60% threshold)
        if num_approved >= num_requested * 0.6:
            print("[Router DSPy] Partial success - finalizing with approved recipes")
            state["warnings"].append(f"Only {num_approved}/{num_requested} recipes approved")
            return "finalize_partial"
        else:
            print("[Router DSPy] Insufficient approved recipes - failing")
            return "handle_failure"

    # Retry available
    print("[Router DSPy] Retrying rejected recipes with DSPy")
    return "retry"


def finalize_meal_plan(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 9: Finalize and save approved recipes.
    """
    print("[Orchestrator DSPy] Finalizing meal plan...")

    approved_recipes = [
        state["generated_recipes"][rid]
        for rid in state["approved_recipe_ids"]
    ]

    # Save to database
    recipe_ids = db_service.save_recipes(state["user_id"], approved_recipes)

    print(f"[Orchestrator DSPy] Saved {len(recipe_ids)} recipes to database")

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

    print(f"[Orchestrator DSPy] ✓ Meal plan complete! {len(approved_recipes)} recipes ready.")

    return state


def handle_failure(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 10: Handle workflow failure gracefully.
    """
    print("[Orchestrator DSPy] ✗ Workflow failed - graceful degradation")

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


# ============================================================================
# BUILD THE DSPY-POWERED GRAPH
# ============================================================================

def create_recipe_generation_graph_dspy():
    """
    Construct the complete DSPy-powered LangGraph workflow.

    This graph uses DSPy 3.0 modules for all LLM interactions:
    - ChainOfThought for Chef planning
    - ChainOfThought for SousChef recipe generation
    - Refine for recipe regeneration
    - ChainOfThought for Nutritionist validation

    Returns:
        Compiled LangGraph StateGraph ready for execution
    """
    workflow = StateGraph(RecipeGenerationState)

    # Add nodes (DSPy-powered)
    workflow.add_node("initialize_chef_dspy", initialize_chef_dspy)
    workflow.add_node("plan_ingredient_groups_dspy", plan_ingredient_groups_dspy)
    workflow.add_node("generate_recipes_parallel_dspy", generate_recipes_parallel_dspy)
    workflow.add_node("sous_chef_generate_dspy", sous_chef_generate_dspy)
    workflow.add_node("aggregate_recipes", aggregate_recipes)
    workflow.add_node("validate_recipes_dspy", validate_recipes_dspy)
    workflow.add_node("handle_rejections_dspy", handle_rejections_dspy)
    workflow.add_node("retry_generation_dspy", retry_generation_dspy)
    workflow.add_node("finalize_meal_plan", finalize_meal_plan)
    workflow.add_node("handle_failure", handle_failure)

    # Define edges
    workflow.set_entry_point("initialize_chef_dspy")
    workflow.add_edge("initialize_chef_dspy", "plan_ingredient_groups_dspy")
    workflow.add_edge("plan_ingredient_groups_dspy", "generate_recipes_parallel_dspy")
    workflow.add_edge("generate_recipes_parallel_dspy", "aggregate_recipes")
    workflow.add_edge("aggregate_recipes", "validate_recipes_dspy")

    # Conditional routing after validation
    workflow.add_conditional_edges(
        "validate_recipes_dspy",
        route_after_validation,
        {
            "finalize": "finalize_meal_plan",
            "retry": "handle_rejections_dspy",
            "finalize_partial": "finalize_meal_plan",
            "handle_failure": "handle_failure"
        }
    )

    # Retry loop (using DSPy Refine for regeneration)
    workflow.add_edge("handle_rejections_dspy", "retry_generation_dspy")
    workflow.add_edge("retry_generation_dspy", "validate_recipes_dspy")

    # Terminal nodes
    workflow.add_edge("finalize_meal_plan", END)
    workflow.add_edge("handle_failure", END)

    print("[DSPy Graph] Compiled DSPy-powered LangGraph workflow")
    return workflow.compile()


# ============================================================================
# CONVENIENCE FUNCTION
# ============================================================================

def run_recipe_generation_dspy(
    user_id: int,
    postal_code: str,
    budget: float,
    household_size: int,
    dietary_restrictions: list,
    num_meals: int,
    preferences: dict = None
):
    """
    Run the DSPy-powered recipe generation workflow.

    This is a convenience function that:
    1. Initializes the state
    2. Creates the DSPy-powered graph
    3. Executes the workflow
    4. Returns the final state

    Args:
        user_id: User identifier
        postal_code: Location for deal fetching
        budget: Total budget in dollars
        household_size: Number of people
        dietary_restrictions: List of restrictions
        num_meals: Number of meals to generate
        preferences: Optional user preferences dict

    Returns:
        Final RecipeGenerationState with generated recipes
    """
    # Initialize state
    initial_state = RecipeGenerationState(
        user_id=user_id,
        postal_code=postal_code,
        budget=budget,
        household_size=household_size,
        dietary_restrictions=dietary_restrictions or [],
        num_meals=num_meals,
        preferences=preferences or {},
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
    graph = create_recipe_generation_graph_dspy()
    final_state = graph.invoke(initial_state)

    return final_state
