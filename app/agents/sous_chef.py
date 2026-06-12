from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
import json
import time
import uuid
from typing import Dict, List
from .state import Recipe, RecipeGenerationState
from .prompts import PromptTemplates
from ..config import settings
from ..services.mlflow_logger import MLflowLogger

def _coerce_recipe_list(data) -> List[Dict]:
    """
    Models don't reliably return a bare JSON array: tolerate a single recipe
    object or a wrapper like {"recipes": [...]}. (Full schema validation with
    retries is the roadmap's "harden LLM output" task.)
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        if "name" in data:
            return [data]
        # {"recipe_1": {...}, "recipe_2": {...}}
        recipe_like = [v for v in data.values() if isinstance(v, dict) and "name" in v]
        if recipe_like:
            return recipe_like
    return []


def _to_recipe(recipe_data: Dict) -> Recipe:
    """
    Build a Recipe from model output, defaulting optional fields.
    Raises KeyError if a required field is missing — callers skip that recipe.
    """
    return Recipe(
        recipe_id=str(uuid.uuid4()),
        name=recipe_data["name"],
        ingredients=recipe_data["ingredients"],
        instructions=recipe_data["instructions"],
        servings=recipe_data.get("servings", 2),
        total_cost=recipe_data.get("total_cost", 0.0),
        estimated_prep_time=recipe_data.get("estimated_prep_time", 30),
        meal_type=recipe_data.get("meal_type", "dinner"),
        cuisine_type=recipe_data.get("cuisine_type")
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
        dietary_restrictions: List[str]
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
            response = self.llm.invoke([HumanMessage(content=prompt)])
            recipes_data = _coerce_recipe_list(json.loads(response.content))

            # Convert to Recipe objects with IDs; skip malformed entries
            # rather than discarding the whole batch
            recipes = []
            for recipe_data in recipes_data:
                try:
                    recipes.append(_to_recipe(recipe_data))
                except KeyError as e:
                    print(f"[{chef_id}] Skipping malformed recipe (missing {e})")

            duration = time.time() - start_time

            # Log to MLflow
            MLflowLogger.log_agent_call(
                agent_name=chef_id,
                tokens=len(response.content),
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
        dietary_restrictions: List[str]
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
            response = self.llm.invoke([HumanMessage(content=prompt)])
            candidates = _coerce_recipe_list(json.loads(response.content))
            if not candidates:
                raise ValueError("no recipe object in model output")

            # Create new recipe with new ID
            recipe = _to_recipe(candidates[0])

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
        dietary_restrictions=task["dietary_restrictions"]
    )

    return {
        "generated_recipes": {r["recipe_id"]: r for r in recipes},
        "sous_chef_assignments": {r["recipe_id"]: chef_id for r in recipes},
    }
