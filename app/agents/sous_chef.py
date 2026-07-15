from langchain_ollama import ChatOllama
import json
import time
import uuid
from typing import Dict, List
from .state import Recipe
from .prompts import PromptTemplates
from .llm_output import GeneratedRecipe, RecipeBatch, invoke_validated
from .costing import price_recipe
from ..config import settings
from ..services.mlflow_logger import MLflowLogger


def _to_recipe(chef_id: str, model: GeneratedRecipe, deal_index: Dict[str, Dict]) -> Recipe:
    """Build a Recipe from validated model output. total_cost is computed in
    Python from the deal index — model arithmetic is not trusted."""
    ingredients = [i.model_dump() for i in model.ingredients]
    pricing = price_recipe(ingredients, deal_index)
    if pricing.unmatched:
        print(f"[{chef_id}] No deal match for {pricing.unmatched} — using model prices for those")

    return Recipe(
        recipe_id=str(uuid.uuid4()),
        name=model.name,
        ingredients=ingredients,
        instructions=model.instructions,
        servings=model.servings,
        total_cost=pricing.total_cost,
        estimated_prep_time=model.estimated_prep_time,
        meal_type=model.meal_type,
        cuisine_type=model.cuisine_type,
    )


class SousChef:
    """SousChef agent for recipe generation."""

    def __init__(self):
        self.llm = ChatOllama(
            model=settings.ollama_sous_chef_model,
            base_url=settings.ollama_base_url,
            temperature=0.8,  # Higher creativity for recipes
            format="json"
        )

    def generate_recipes(
        self,
        chef_id: str,
        ingredient_group: List[Dict],
        target_recipe_count: int,
        household_size: int,
        dietary_restrictions: List[str],
        deal_index: Dict[str, Dict],
    ) -> List[Recipe]:
        """
        Generate recipes from assigned ingredients.

        This is the parallel worker node.
        """
        print(f"[{chef_id}] Generating {target_recipe_count} recipes...")

        start_time = time.time()

        # Prepare prompt
        prompt = PromptTemplates.SOUS_CHEF_RECIPE_GENERATION.format(
            ingredients_json=json.dumps(ingredient_group, indent=2),
            target_recipe_count=target_recipe_count,
            servings=household_size,
            dietary_restrictions=", ".join(dietary_restrictions)
        )

        try:
            batch, raw = invoke_validated(self.llm, prompt, RecipeBatch)
            recipes = [_to_recipe(chef_id, r, deal_index) for r in batch.recipes]

            duration = time.time() - start_time

            # Log to MLflow
            MLflowLogger.log_agent_call(
                agent_name=chef_id,
                tokens=len(raw),
                duration=duration,
                model=settings.ollama_sous_chef_model,
                success=True
            )

            print(f"[{chef_id}] Generated {len(recipes)} recipes in {duration:.2f}s")

            return recipes

        except Exception as e:
            print(f"[{chef_id}] ERROR: {e}")
            return []

    def regenerate_with_feedback(
        self,
        chef_id: str,
        original_recipe: Recipe,
        feedback: str,
        ingredient_group: List[Dict],
        household_size: int,
        dietary_restrictions: List[str],
        deal_index: Dict[str, Dict],
    ) -> Recipe:
        """
        Regenerate a rejected recipe with Nutritionist feedback.
        """
        print(f"[{chef_id}] Regenerating recipe with feedback...")

        prompt = PromptTemplates.SOUS_CHEF_RETRY_WITH_FEEDBACK.format(
            original_recipe_json=json.dumps(original_recipe, indent=2),
            feedback=feedback,
            ingredients_json=json.dumps(ingredient_group, indent=2),
            servings=household_size
        )

        try:
            batch, _raw = invoke_validated(self.llm, prompt, RecipeBatch)

            # Create new recipe with new ID
            recipe = _to_recipe(chef_id, batch.recipes[0], deal_index)

            print(f"[{chef_id}] Regenerated: {recipe['name']}")
            return recipe

        except Exception as e:
            print(f"[{chef_id}] Regeneration ERROR: {e}")
            return None


def sous_chef_generate_node(task: dict) -> dict:
    """
    Node 4: Individual SousChef worker node for parallel execution.

    Invoked via Send() from the fan-out edge; `task` carries the worker's
    assignment (chef_id, ingredient_group, target_recipe_count) on top of the
    shared input fields. Returns only this worker's recipes — the merge_dicts
    reducers on generated_recipes/sous_chef_assignments combine the three
    parallel workers' outputs.
    """
    chef_id = task.get("chef_id", "sous_chef_unknown")
    ingredient_group = task.get("ingredient_group", [])
    target_recipe_count = task.get("target_recipe_count", 2)

    sous_chef = SousChef()

    recipes = sous_chef.generate_recipes(
        chef_id=chef_id,
        ingredient_group=ingredient_group,
        target_recipe_count=target_recipe_count,
        household_size=task["household_size"],
        dietary_restrictions=task["dietary_restrictions"],
        deal_index=task.get("deal_index", {}),
    )

    return {
        "generated_recipes": {r["recipe_id"]: r for r in recipes},
        "sous_chef_assignments": {r["recipe_id"]: chef_id for r in recipes},
    }
