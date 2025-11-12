"""
DSPy + LangGraph Integration Layer

This module provides wrappers that integrate DSPy 3.0 modules with LangGraph nodes,
maintaining the existing StateGraph architecture while leveraging DSPy's declarative
programming model.

The integration allows for:
- Seamless state management through LangGraph
- Declarative LM programming through DSPy
- Parallel execution capabilities
- Automatic prompt optimization potential with DSPy optimizers
"""

import dspy
import json
import time
from typing import Dict, Any
from langgraph.types import Send

from .state import RecipeGenerationState, Recipe, ValidationResult
from .dspy_modules import ChefOrchestratorDSPy, SousChefDSPy, NutritionistDSPy
from .dspy_config import DSPyConfig
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger


# ============================================================================
# INITIALIZE DSPY AGENTS
# ============================================================================

# Configure DSPy with all language models
lm_configs = DSPyConfig.setup_all_agents()

# Initialize DSPy agent modules
dspy_chef = ChefOrchestratorDSPy()
dspy_sous_chef = SousChefDSPy()
dspy_nutritionist = NutritionistDSPy()

# Initialize services
db_service = DatabaseService()


# ============================================================================
# CHEF ORCHESTRATOR NODES (DSPy-powered)
# ============================================================================

def initialize_chef_dspy(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 1: Initialize Chef by fetching deals and setting up workflow.
    Uses existing database service (no LLM needed).
    """
    print(f"[Chef DSPy] Initializing for user {state['user_id']}...")

    # Start MLflow run
    mlflow_run_id = MLflowLogger.start_run(
        user_id=state["user_id"],
        num_meals=state["num_meals"],
        budget=state["budget"],
        dietary_restrictions=state["dietary_restrictions"]
    )

    # Fetch deals from database
    deals = db_service.fetch_current_deals(state["postal_code"])

    # Create deal index for O(1) lookups
    deal_index = {deal["product_name"]: deal for deal in deals}

    # Update state
    state["mlflow_run_id"] = mlflow_run_id
    state["available_deals"] = deals
    state["deal_index"] = deal_index
    state["status"] = "planning"
    state["iteration_count"] = 0
    state["max_iterations"] = 2
    state["agent_call_log"] = []
    state["errors"] = []
    state["warnings"] = []
    state["generated_recipes"] = {}
    state["sous_chef_assignments"] = {}
    state["validation_results"] = {}
    state["approved_recipe_ids"] = []
    state["rejected_recipe_ids"] = []
    state["recipes_pending_retry"] = {}

    MLflowLogger.log_agent_call(
        mlflow_run_id, "chef", "initialize", len(deals)
    )

    print(f"[Chef DSPy] Found {len(deals)} deals for postal code {state['postal_code']}")
    return state


def plan_ingredient_groups_dspy(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 2: Plan ingredient groups using DSPy ChainOfThought.
    """
    print("[Chef DSPy] Planning ingredient groups with DSPy ChainOfThought...")
    start_time = time.time()

    # Configure DSPy with Chef LM
    dspy.settings.configure(lm=lm_configs["chef"])

    try:
        # Call DSPy module
        ingredient_groups, ingredient_reuse_map, reasoning = dspy_chef.plan_ingredients(
            available_deals=state["available_deals"],
            budget=state["budget"],
            num_meals=state["num_meals"],
            household_size=state["household_size"],
            dietary_restrictions=state["dietary_restrictions"],
            preferences=state.get("preferences", {}),
        )

        # Update state
        state["ingredient_groups"] = ingredient_groups
        state["ingredient_reuse_map"] = ingredient_reuse_map
        state["target_ingredients_per_group"] = len(state["available_deals"]) // 3

        # Log to MLflow
        elapsed = time.time() - start_time
        MLflowLogger.log_agent_call(
            state["mlflow_run_id"], "chef", "plan_ingredients", elapsed
        )

        # Log reasoning to agent call log
        state["agent_call_log"].append({
            "agent": "chef",
            "action": "plan_ingredients",
            "reasoning": reasoning,
            "elapsed_time": elapsed,
        })

        print(f"[Chef DSPy] Created 3 ingredient groups (reasoning: {reasoning[:100]}...)")
        return state

    except Exception as e:
        state["errors"].append(f"Chef ingredient planning failed: {str(e)}")
        state["status"] = "failed"
        print(f"[Chef DSPy] ERROR: {e}")
        return state


def handle_rejections_dspy(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 7: Handle rejections using DSPy ChainOfThought.
    """
    print("[Chef DSPy] Analyzing rejections with DSPy ChainOfThought...")
    start_time = time.time()

    # Configure DSPy with Chef LM
    dspy.settings.configure(lm=lm_configs["chef"])

    try:
        # Prepare rejected recipes and feedback
        rejected_recipes = [
            state["generated_recipes"][recipe_id]
            for recipe_id in state["rejected_recipe_ids"]
        ]
        validation_feedback = [
            state["validation_results"][recipe_id]
            for recipe_id in state["rejected_recipe_ids"]
        ]

        # Call DSPy module
        retry_strategy, retry_assignments, reasoning = dspy_chef.handle_rejections(
            rejected_recipes=rejected_recipes,
            validation_feedback=validation_feedback,
            current_iteration=state["iteration_count"],
            max_iterations=state["max_iterations"],
            available_deals=state["available_deals"],
        )

        # Update state
        state["retry_strategy"] = retry_strategy
        state["recipes_pending_retry"] = retry_assignments

        # Log to MLflow
        elapsed = time.time() - start_time
        MLflowLogger.log_agent_call(
            state["mlflow_run_id"], "chef", "handle_rejections", elapsed
        )

        # Log reasoning
        state["agent_call_log"].append({
            "agent": "chef",
            "action": "handle_rejections",
            "reasoning": reasoning,
            "retry_strategy": retry_strategy,
            "elapsed_time": elapsed,
        })

        print(f"[Chef DSPy] Retry strategy: {retry_strategy} (reasoning: {reasoning[:100]}...)")
        return state

    except Exception as e:
        state["errors"].append(f"Chef rejection handling failed: {str(e)}")
        print(f"[Chef DSPy] ERROR: {e}")
        return state


# ============================================================================
# SOUS CHEF NODES (DSPy-powered with parallel execution)
# ============================================================================

def sous_chef_generate_dspy(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 4: Generate recipes using DSPy ChainOfThought.
    This is called in parallel for each sous chef.
    """
    chef_id = state.get("chef_id", "sous_chef_1")
    ingredient_group = state.get("ingredient_group", [])
    target_recipe_count = state.get("target_recipe_count", 1)

    print(f"[{chef_id} DSPy] Generating {target_recipe_count} recipes with DSPy...")
    start_time = time.time()

    # Configure DSPy with Sous Chef LM
    dspy.settings.configure(lm=lm_configs["sous_chef"])

    try:
        # Call DSPy module
        recipes, ingredient_usage, creative_notes = dspy_sous_chef.generate_recipes(
            chef_id=chef_id,
            ingredient_group=ingredient_group,
            target_recipe_count=target_recipe_count,
            household_size=state["household_size"],
            dietary_restrictions=state["dietary_restrictions"],
            preferences=state.get("preferences", {}),
        )

        # Convert to Recipe TypedDict format
        for recipe in recipes:
            recipe_id = recipe.get("recipe_id", f"{chef_id}_{recipe['name']}")
            recipe["recipe_id"] = recipe_id

            # Store in state
            if "generated_recipes" not in state:
                state["generated_recipes"] = {}
            if "sous_chef_assignments" not in state:
                state["sous_chef_assignments"] = {}

            state["generated_recipes"][recipe_id] = recipe
            state["sous_chef_assignments"][recipe_id] = chef_id

        # Log to MLflow
        elapsed = time.time() - start_time
        MLflowLogger.log_agent_call(
            state["mlflow_run_id"], chef_id, "generate_recipes", elapsed
        )

        # Log creative notes
        state["agent_call_log"].append({
            "agent": chef_id,
            "action": "generate_recipes",
            "creative_notes": creative_notes,
            "elapsed_time": elapsed,
        })

        print(f"[{chef_id} DSPy] Generated {len(recipes)} recipes (notes: {creative_notes[:100]}...)")
        return state

    except Exception as e:
        if "errors" not in state:
            state["errors"] = []
        state["errors"].append(f"{chef_id} recipe generation failed: {str(e)}")
        print(f"[{chef_id} DSPy] ERROR: {e}")
        return state


# ============================================================================
# NUTRITIONIST NODES (DSPy-powered)
# ============================================================================

def validate_recipes_dspy(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 6: Validate recipes using DSPy ChainOfThought.
    """
    print("[Nutritionist DSPy] Validating recipes with DSPy ChainOfThought...")
    start_time = time.time()

    # Configure DSPy with Nutritionist LM
    dspy.settings.configure(lm=lm_configs["nutritionist"])

    try:
        # Prepare recipes for validation
        recipes = list(state["generated_recipes"].values())

        # Call DSPy module
        validation_results, overall_assessment, recommendations = dspy_nutritionist.validate_recipes(
            recipes=recipes,
            dietary_restrictions=state["dietary_restrictions"],
            household_size=state["household_size"],
        )

        # Update state with validation results
        state["validation_results"] = {}
        state["approved_recipe_ids"] = []
        state["rejected_recipe_ids"] = []

        for result in validation_results:
            recipe_id = result["recipe_id"]
            state["validation_results"][recipe_id] = result

            if result["approved"]:
                state["approved_recipe_ids"].append(recipe_id)
            else:
                state["rejected_recipe_ids"].append(recipe_id)

        # Increment iteration count
        state["iteration_count"] += 1
        state["status"] = "validating"

        # Log to MLflow
        elapsed = time.time() - start_time
        MLflowLogger.log_agent_call(
            state["mlflow_run_id"], "nutritionist", "validate_recipes", elapsed
        )

        # Log assessment
        state["agent_call_log"].append({
            "agent": "nutritionist",
            "action": "validate_recipes",
            "overall_assessment": overall_assessment,
            "recommendations": recommendations,
            "elapsed_time": elapsed,
        })

        print(f"[Nutritionist DSPy] Approved: {len(state['approved_recipe_ids'])}, "
              f"Rejected: {len(state['rejected_recipe_ids'])}")
        print(f"[Nutritionist DSPy] Assessment: {overall_assessment[:100]}...")

        return state

    except Exception as e:
        state["errors"].append(f"Nutritionist validation failed: {str(e)}")
        state["status"] = "failed"
        print(f"[Nutritionist DSPy] ERROR: {e}")
        return state


# ============================================================================
# PARALLEL EXECUTION HELPER
# ============================================================================

def generate_recipes_parallel_dspy(state: RecipeGenerationState):
    """
    Node 3: Fan-out to 3 parallel SousChef DSPy nodes.
    """
    print("[Orchestrator DSPy] Distributing work to 3 SousChefs (DSPy-powered)...")

    # Calculate recipes per chef
    num_meals = state["num_meals"]
    recipes_per_chef = [
        (num_meals // 3) + (1 if i < num_meals % 3 else 0)
        for i in range(3)
    ]

    # Create Send objects for parallel execution
    return [
        Send("sous_chef_generate_dspy", {
            **state,
            "chef_id": f"sous_chef_{i+1}",
            "ingredient_group": state["ingredient_groups"][i],
            "target_recipe_count": recipes_per_chef[i]
        })
        for i in range(3)
    ]


# ============================================================================
# REGENERATION NODE
# ============================================================================

def retry_generation_dspy(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 8: Regenerate rejected recipes with feedback using DSPy Refine.
    """
    print("[Orchestrator DSPy] Regenerating rejected recipes with DSPy Refine...")

    # Configure DSPy with Sous Chef LM
    dspy.settings.configure(lm=lm_configs["sous_chef"])

    for recipe_id in state["rejected_recipe_ids"]:
        try:
            # Get original recipe and feedback
            original_recipe = state["generated_recipes"][recipe_id]
            feedback = state["validation_results"][recipe_id]["feedback"]

            # Get chef and ingredient group
            chef_id = state["sous_chef_assignments"][recipe_id]
            chef_idx = int(chef_id.split("_")[-1]) - 1
            ingredient_group = state["ingredient_groups"][chef_idx]

            # Call DSPy module to regenerate
            improved_recipe, improvements = dspy_sous_chef.regenerate_recipe(
                chef_id=chef_id,
                original_recipe=original_recipe,
                validation_feedback=feedback,
                ingredient_group=ingredient_group,
                household_size=state["household_size"],
                dietary_restrictions=state["dietary_restrictions"],
            )

            # Update recipe in state
            improved_recipe["recipe_id"] = recipe_id
            state["generated_recipes"][recipe_id] = improved_recipe

            # Log improvements
            state["agent_call_log"].append({
                "agent": chef_id,
                "action": "regenerate_recipe",
                "recipe_id": recipe_id,
                "improvements": improvements,
            })

            print(f"[{chef_id} DSPy] Regenerated {recipe_id}: {improvements[:100]}...")

        except Exception as e:
            state["errors"].append(f"Regeneration failed for {recipe_id}: {str(e)}")
            print(f"[DSPy] ERROR regenerating {recipe_id}: {e}")

    state["status"] = "retrying"
    return state
