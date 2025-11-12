"""
DSPy 3.0 Signature definitions for the grocery optimizer multi-agent system.

This module defines declarative signatures for:
- Chef Orchestrator (ingredient planning)
- SousChef (recipe generation)
- Nutritionist (recipe validation)
"""

import dspy
from typing import List, Dict


# ============================================================================
# CHEF ORCHESTRATOR SIGNATURES
# ============================================================================

class IngredientPlanning(dspy.Signature):
    """Plan optimal ingredient groups for multiple sous chefs to create recipes.

    This signature divides available grocery deals into balanced groups that:
    - Maximize ingredient variety and reuse
    - Balance cost across groups
    - Consider dietary restrictions
    - Enable parallel recipe generation
    """

    available_deals: str = dspy.InputField(
        desc="JSON array of grocery deals with name, price, store, quantity"
    )
    budget: float = dspy.InputField(
        desc="Total budget in dollars for all meals"
    )
    num_meals: int = dspy.InputField(
        desc="Number of meals to generate"
    )
    household_size: int = dspy.InputField(
        desc="Number of people in household"
    )
    dietary_restrictions: str = dspy.InputField(
        desc="Comma-separated list of dietary restrictions (e.g., 'vegetarian, gluten-free')"
    )
    preferences: str = dspy.InputField(
        desc="Additional user preferences in JSON format"
    )

    ingredient_groups: str = dspy.OutputField(
        desc="JSON array of 3 ingredient groups, each containing selected deals"
    )
    ingredient_reuse_map: str = dspy.OutputField(
        desc="JSON object mapping ingredient names to reuse count across groups"
    )
    reasoning: str = dspy.OutputField(
        desc="Explanation of how ingredients were grouped and optimized"
    )


class RejectionHandling(dspy.Signature):
    """Analyze rejected recipes and determine optimal retry strategy.

    This signature processes nutritionist feedback to decide whether to:
    - Reassign recipes to different sous chefs
    - Select new ingredient combinations
    """

    rejected_recipes: str = dspy.InputField(
        desc="JSON array of rejected recipes with IDs and details"
    )
    validation_feedback: str = dspy.InputField(
        desc="JSON array of validation results with specific feedback"
    )
    current_iteration: int = dspy.InputField(
        desc="Current retry iteration number"
    )
    max_iterations: int = dspy.InputField(
        desc="Maximum allowed retry iterations"
    )
    available_deals: str = dspy.InputField(
        desc="JSON array of remaining available grocery deals"
    )

    retry_strategy: str = dspy.OutputField(
        desc="Either 'reassign_chef' or 'new_ingredients'"
    )
    retry_assignments: str = dspy.OutputField(
        desc="JSON object mapping recipe IDs to new chef IDs or ingredient groups"
    )
    reasoning: str = dspy.OutputField(
        desc="Explanation of why this retry strategy was chosen"
    )


# ============================================================================
# SOUS CHEF SIGNATURES
# ============================================================================

class RecipeGeneration(dspy.Signature):
    """Generate creative, budget-friendly recipes from assigned ingredients.

    This signature creates complete recipes that:
    - Use all or most assigned ingredients
    - Meet dietary restrictions
    - Fit within budget constraints
    - Are practical and appealing
    """

    chef_id: str = dspy.InputField(
        desc="Identifier for this sous chef (e.g., 'sous_chef_1')"
    )
    ingredient_group: str = dspy.InputField(
        desc="JSON array of assigned grocery deals/ingredients"
    )
    target_recipe_count: int = dspy.InputField(
        desc="Number of recipes to generate from these ingredients"
    )
    household_size: int = dspy.InputField(
        desc="Number of servings needed per recipe"
    )
    dietary_restrictions: str = dspy.InputField(
        desc="Comma-separated dietary restrictions to respect"
    )
    preferences: str = dspy.InputField(
        desc="User preferences for cuisine types and meal types"
    )

    recipes: str = dspy.OutputField(
        desc="JSON array of complete recipes with name, ingredients, instructions, cost, servings"
    )
    ingredient_usage: str = dspy.OutputField(
        desc="JSON object showing which ingredients were used in which recipes"
    )
    creative_notes: str = dspy.OutputField(
        desc="Brief explanation of recipe choices and creative decisions"
    )


class RecipeRegeneration(dspy.Signature):
    """Regenerate a recipe based on nutritionist feedback.

    This signature improves a rejected recipe by:
    - Addressing specific nutritional concerns
    - Maintaining dietary compliance
    - Using the same ingredient group
    - Keeping within budget
    """

    chef_id: str = dspy.InputField(
        desc="Identifier for this sous chef"
    )
    original_recipe: str = dspy.InputField(
        desc="JSON object of the original rejected recipe"
    )
    validation_feedback: str = dspy.InputField(
        desc="Specific feedback from nutritionist about what to improve"
    )
    ingredient_group: str = dspy.InputField(
        desc="JSON array of available ingredients to use"
    )
    household_size: int = dspy.InputField(
        desc="Number of servings needed"
    )
    dietary_restrictions: str = dspy.InputField(
        desc="Dietary restrictions to respect"
    )

    improved_recipe: str = dspy.OutputField(
        desc="JSON object of the regenerated recipe with improvements"
    )
    improvements_made: str = dspy.OutputField(
        desc="Explanation of specific changes made to address feedback"
    )


# ============================================================================
# NUTRITIONIST SIGNATURES
# ============================================================================

class RecipeValidation(dspy.Signature):
    """Validate recipes for nutritional balance, safety, and dietary compliance.

    This signature provides comprehensive validation checking:
    - Nutritional balance (macros, vitamins, minerals)
    - Allergen safety
    - Dietary restriction compliance
    - Portion appropriateness
    - Health score (0-100)
    """

    recipes: str = dspy.InputField(
        desc="JSON array of recipes to validate, each with ingredients and instructions"
    )
    dietary_restrictions: str = dspy.InputField(
        desc="Comma-separated dietary restrictions to enforce"
    )
    household_size: int = dspy.InputField(
        desc="Number of people per household for portion analysis"
    )

    validation_results: str = dspy.OutputField(
        desc="JSON array of validation results, one per recipe, with recipe_id, approved (bool), feedback, nutrition_facts, dietary_compliance, health_score"
    )
    overall_assessment: str = dspy.OutputField(
        desc="Summary of overall meal plan quality and balance"
    )
    recommendations: str = dspy.OutputField(
        desc="Suggestions for improving the overall meal plan"
    )
