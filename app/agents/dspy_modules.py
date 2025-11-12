"""
DSPy 3.0 Module implementations for the grocery optimizer multi-agent system.

This module provides DSPy-based implementations of:
- ChefOrchestratorDSPy: Plans ingredient groups and handles rejections
- SousChefDSPy: Generates and regenerates recipes
- NutritionistDSPy: Validates recipes for nutrition and compliance

These modules use DSPy's declarative programming model and can be integrated
with LangGraph for stateful workflow orchestration.
"""

import dspy
import json
from typing import List, Dict, Optional
from app.agents.dspy_signatures import (
    IngredientPlanning,
    RejectionHandling,
    RecipeGeneration,
    RecipeRegeneration,
    RecipeValidation,
)


# ============================================================================
# CHEF ORCHESTRATOR DSPY MODULE
# ============================================================================

class ChefOrchestratorDSPy(dspy.Module):
    """
    Chef Orchestrator agent using DSPy's ChainOfThought for complex planning.

    This module handles:
    - Ingredient group planning with cost optimization
    - Rejection analysis and retry strategy determination
    """

    def __init__(self):
        super().__init__()
        # Use ChainOfThought for complex reasoning about ingredient optimization
        self.ingredient_planner = dspy.ChainOfThought(IngredientPlanning)
        # Use ChainOfThought for analyzing rejections and planning retries
        self.rejection_handler = dspy.ChainOfThought(RejectionHandling)

    def plan_ingredients(
        self,
        available_deals: List[Dict],
        budget: float,
        num_meals: int,
        household_size: int,
        dietary_restrictions: List[str],
        preferences: Dict,
    ) -> tuple[List[List[Dict]], Dict[str, int], str]:
        """
        Plan ingredient groups for sous chefs.

        Returns:
            - ingredient_groups: List of 3 ingredient groups
            - ingredient_reuse_map: Dict mapping ingredient names to reuse count
            - reasoning: Explanation of planning decisions
        """
        # Prepare inputs
        deals_json = json.dumps(available_deals)
        restrictions_str = ", ".join(dietary_restrictions) if dietary_restrictions else "none"
        prefs_json = json.dumps(preferences) if preferences else "{}"

        # Execute DSPy module
        result = self.ingredient_planner(
            available_deals=deals_json,
            budget=budget,
            num_meals=num_meals,
            household_size=household_size,
            dietary_restrictions=restrictions_str,
            preferences=prefs_json,
        )

        # Parse outputs
        ingredient_groups = json.loads(result.ingredient_groups)
        ingredient_reuse_map = json.loads(result.ingredient_reuse_map)
        reasoning = result.reasoning

        return ingredient_groups, ingredient_reuse_map, reasoning

    def handle_rejections(
        self,
        rejected_recipes: List[Dict],
        validation_feedback: List[Dict],
        current_iteration: int,
        max_iterations: int,
        available_deals: List[Dict],
    ) -> tuple[str, Dict[str, str], str]:
        """
        Analyze rejections and determine retry strategy.

        Returns:
            - retry_strategy: Either 'reassign_chef' or 'new_ingredients'
            - retry_assignments: Dict mapping recipe IDs to new assignments
            - reasoning: Explanation of strategy choice
        """
        # Prepare inputs
        rejected_json = json.dumps(rejected_recipes)
        feedback_json = json.dumps(validation_feedback)
        deals_json = json.dumps(available_deals)

        # Execute DSPy module
        result = self.rejection_handler(
            rejected_recipes=rejected_json,
            validation_feedback=feedback_json,
            current_iteration=current_iteration,
            max_iterations=max_iterations,
            available_deals=deals_json,
        )

        # Parse outputs
        retry_strategy = result.retry_strategy
        retry_assignments = json.loads(result.retry_assignments)
        reasoning = result.reasoning

        return retry_strategy, retry_assignments, reasoning


# ============================================================================
# SOUS CHEF DSPY MODULE
# ============================================================================

class SousChefDSPy(dspy.Module):
    """
    Sous Chef agent using DSPy's ChainOfThought for creative recipe generation.

    This module handles:
    - Initial recipe generation from ingredient groups
    - Recipe regeneration based on nutritionist feedback
    """

    def __init__(self):
        super().__init__()
        # Use ChainOfThought for creative recipe generation
        self.recipe_generator = dspy.ChainOfThought(RecipeGeneration)
        # Use Refine for iterative improvement based on feedback
        self.recipe_regenerator = dspy.Refine(RecipeRegeneration)

    def generate_recipes(
        self,
        chef_id: str,
        ingredient_group: List[Dict],
        target_recipe_count: int,
        household_size: int,
        dietary_restrictions: List[str],
        preferences: Dict,
    ) -> tuple[List[Dict], Dict[str, List[str]], str]:
        """
        Generate recipes from assigned ingredients.

        Returns:
            - recipes: List of recipe dictionaries
            - ingredient_usage: Dict mapping ingredients to recipe IDs
            - creative_notes: Explanation of creative choices
        """
        # Prepare inputs
        ingredients_json = json.dumps(ingredient_group)
        restrictions_str = ", ".join(dietary_restrictions) if dietary_restrictions else "none"
        prefs_json = json.dumps(preferences) if preferences else "{}"

        # Execute DSPy module
        result = self.recipe_generator(
            chef_id=chef_id,
            ingredient_group=ingredients_json,
            target_recipe_count=target_recipe_count,
            household_size=household_size,
            dietary_restrictions=restrictions_str,
            preferences=prefs_json,
        )

        # Parse outputs
        recipes = json.loads(result.recipes)
        ingredient_usage = json.loads(result.ingredient_usage)
        creative_notes = result.creative_notes

        return recipes, ingredient_usage, creative_notes

    def regenerate_recipe(
        self,
        chef_id: str,
        original_recipe: Dict,
        validation_feedback: str,
        ingredient_group: List[Dict],
        household_size: int,
        dietary_restrictions: List[str],
    ) -> tuple[Dict, str]:
        """
        Regenerate a recipe based on feedback.

        Returns:
            - improved_recipe: Updated recipe dictionary
            - improvements_made: Explanation of changes
        """
        # Prepare inputs
        recipe_json = json.dumps(original_recipe)
        ingredients_json = json.dumps(ingredient_group)
        restrictions_str = ", ".join(dietary_restrictions) if dietary_restrictions else "none"

        # Execute DSPy module with Refine (allows multiple improvement iterations)
        result = self.recipe_regenerator(
            chef_id=chef_id,
            original_recipe=recipe_json,
            validation_feedback=validation_feedback,
            ingredient_group=ingredients_json,
            household_size=household_size,
            dietary_restrictions=restrictions_str,
        )

        # Parse outputs
        improved_recipe = json.loads(result.improved_recipe)
        improvements_made = result.improvements_made

        return improved_recipe, improvements_made

    def batch_generate_recipes(
        self,
        chef_configs: List[Dict],
    ) -> List[Dict]:
        """
        Generate recipes in parallel using DSPy's batch method.

        Args:
            chef_configs: List of dicts, each containing:
                - chef_id
                - ingredient_group
                - target_recipe_count
                - household_size
                - dietary_restrictions
                - preferences

        Returns:
            List of results, one per chef configuration
        """
        # Prepare batch inputs
        batch_inputs = []
        for config in chef_configs:
            restrictions_str = ", ".join(config["dietary_restrictions"]) if config["dietary_restrictions"] else "none"
            prefs_json = json.dumps(config.get("preferences", {}))
            batch_inputs.append({
                "chef_id": config["chef_id"],
                "ingredient_group": json.dumps(config["ingredient_group"]),
                "target_recipe_count": config["target_recipe_count"],
                "household_size": config["household_size"],
                "dietary_restrictions": restrictions_str,
                "preferences": prefs_json,
            })

        # Execute in parallel using DSPy's batch method
        batch_results = self.recipe_generator.batch(batch_inputs)

        # Parse results
        results = []
        for result in batch_results:
            results.append({
                "recipes": json.loads(result.recipes),
                "ingredient_usage": json.loads(result.ingredient_usage),
                "creative_notes": result.creative_notes,
            })

        return results


# ============================================================================
# NUTRITIONIST DSPY MODULE
# ============================================================================

class NutritionistDSPy(dspy.Module):
    """
    Nutritionist agent using DSPy's Predict for validation.

    This module handles:
    - Comprehensive recipe validation
    - Nutritional analysis
    - Dietary compliance checking
    """

    def __init__(self):
        super().__init__()
        # Use ChainOfThought for detailed nutritional analysis
        self.validator = dspy.ChainOfThought(RecipeValidation)

    def validate_recipes(
        self,
        recipes: List[Dict],
        dietary_restrictions: List[str],
        household_size: int,
    ) -> tuple[List[Dict], str, str]:
        """
        Validate recipes for nutrition and compliance.

        Returns:
            - validation_results: List of validation result dicts
            - overall_assessment: Summary of meal plan quality
            - recommendations: Suggestions for improvement
        """
        # Prepare inputs
        recipes_json = json.dumps(recipes)
        restrictions_str = ", ".join(dietary_restrictions) if dietary_restrictions else "none"

        # Execute DSPy module
        result = self.validator(
            recipes=recipes_json,
            dietary_restrictions=restrictions_str,
            household_size=household_size,
        )

        # Parse outputs
        validation_results = json.loads(result.validation_results)
        overall_assessment = result.overall_assessment
        recommendations = result.recommendations

        return validation_results, overall_assessment, recommendations


# ============================================================================
# FACTORY FUNCTION
# ============================================================================

def create_dspy_agents():
    """
    Factory function to create all DSPy agent modules.

    Returns:
        Dictionary containing initialized DSPy agent modules
    """
    return {
        "chef": ChefOrchestratorDSPy(),
        "sous_chef": SousChefDSPy(),
        "nutritionist": NutritionistDSPy(),
    }
