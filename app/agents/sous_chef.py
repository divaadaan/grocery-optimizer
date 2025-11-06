from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
import json
import time
import uuid
from typing import Dict, List
from .state import Recipe, RecipeGenerationState
from .prompts import PromptTemplates
from ..services.mlflow_logger import MLflowLogger

class SousChef:
    """SousChef agent using SmolLM-360M for recipe generation."""

    def __init__(self):
        self.llm = ChatOllama(
            model="smollm:360m",
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
            recipes_data = json.loads(response.content)

            # Convert to Recipe objects with IDs
            recipes = []
            for recipe_data in recipes_data:
                recipe = Recipe(
                    recipe_id=str(uuid.uuid4()),
                    name=recipe_data["name"],
                    ingredients=recipe_data["ingredients"],
                    instructions=recipe_data["instructions"],
                    servings=recipe_data["servings"],
                    total_cost=recipe_data["total_cost"],
                    estimated_prep_time=recipe_data.get("estimated_prep_time", 30),
                    meal_type=recipe_data["meal_type"],
                    cuisine_type=recipe_data.get("cuisine_type")
                )
                recipes.append(recipe)

            duration = time.time() - start_time

            # Log to MLflow
            MLflowLogger.log_agent_call(
                agent_name=chef_id,
                tokens=len(response.content),
                duration=duration,
                model="smollm:360m",
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
            recipe_data = json.loads(response.content)

            # Create new recipe with new ID
            recipe = Recipe(
                recipe_id=str(uuid.uuid4()),
                name=recipe_data["name"],
                ingredients=recipe_data["ingredients"],
                instructions=recipe_data["instructions"],
                servings=recipe_data["servings"],
                total_cost=recipe_data["total_cost"],
                estimated_prep_time=recipe_data.get("estimated_prep_time", 30),
                meal_type=recipe_data["meal_type"],
                cuisine_type=recipe_data.get("cuisine_type")
            )

            print(f"[{chef_id}] Regenerated: {recipe['name']}")
            return recipe

        except Exception as e:
            print(f"[{chef_id}] Regeneration ERROR: {e}")
            return None


def sous_chef_generate_node(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 4: Individual SousChef worker node for parallel execution.

    This node is called via Send() with subset of state.
    """
    chef_id = state.get("chef_id", "sous_chef_unknown")
    ingredient_group = state.get("ingredient_group", [])
    target_recipe_count = state.get("target_recipe_count", 2)

    sous_chef = SousChef()

    recipes = sous_chef.generate_recipes(
        chef_id=chef_id,
        ingredient_group=ingredient_group,
        target_recipe_count=target_recipe_count,
        household_size=state["household_size"],
        dietary_restrictions=state["dietary_restrictions"]
    )

    # Update state with generated recipes
    for recipe in recipes:
        state["generated_recipes"][recipe["recipe_id"]] = recipe
        state["sous_chef_assignments"][recipe["recipe_id"]] = chef_id

    return state
