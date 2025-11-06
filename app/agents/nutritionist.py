from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
import json
import time
from typing import Dict
from .state import RecipeGenerationState, ValidationResult
from .prompts import PromptTemplates
from ..services.mlflow_logger import MLflowLogger

class Nutritionist:
    """Nutritionist agent using SmolLM-360M for validation."""

    def __init__(self):
        self.llm = ChatOllama(
            model="smollm:360m",
            temperature=0.3,  # Lower temperature for consistent validation
            format="json"
        )

    def validate_recipes(self, state: RecipeGenerationState) -> RecipeGenerationState:
        """
        Node 6: Validate all generated recipes.
        """
        print(f"[Nutritionist] Validating {len(state['generated_recipes'])} recipes...")

        state["status"] = "validating"
        validation_results = {}
        approved_ids = []
        rejected_ids = []

        for recipe_id, recipe in state["generated_recipes"].items():
            # Skip already validated recipes in retry scenarios
            if recipe_id in state["validation_results"]:
                continue

            start_time = time.time()

            # Prepare prompt
            prompt = PromptTemplates.NUTRITIONIST_VALIDATION.format(
                recipe_json=json.dumps(recipe, indent=2),
                dietary_restrictions=", ".join(state["dietary_restrictions"])
            )

            try:
                response = self.llm.invoke([HumanMessage(content=prompt)])
                validation_data = json.loads(response.content)

                # Create ValidationResult
                result = ValidationResult(
                    recipe_id=recipe_id,
                    approved=validation_data["approved"],
                    feedback=validation_data["feedback"],
                    nutrition_facts=validation_data["nutrition_facts"],
                    dietary_compliance=validation_data["dietary_compliance"],
                    health_score=validation_data["health_score"]
                )

                validation_results[recipe_id] = result

                if result["approved"]:
                    approved_ids.append(recipe_id)
                    print(f"[Nutritionist] ✓ APPROVED: {recipe['name']} (score: {result['health_score']})")
                else:
                    rejected_ids.append(recipe_id)
                    print(f"[Nutritionist] ✗ REJECTED: {recipe['name']}")
                    print(f"  Reason: {result['feedback']}")

                duration = time.time() - start_time

                # Log to MLflow
                state["agent_call_log"].append({
                    "agent": "Nutritionist",
                    "action": "validate_recipe",
                    "recipe_id": recipe_id,
                    "approved": result["approved"],
                    "timestamp": time.time(),
                    "duration": duration
                })

            except Exception as e:
                print(f"[Nutritionist] ERROR validating {recipe['name']}: {e}")
                state["errors"].append(f"Validation error for {recipe_id}: {str(e)}")
                # Graceful degradation: mark as pending
                rejected_ids.append(recipe_id)

        # Update state
        state["validation_results"].update(validation_results)
        state["approved_recipe_ids"].extend(approved_ids)
        state["rejected_recipe_ids"].extend(rejected_ids)

        # Log aggregate metrics
        MLflowLogger.log_validation_results(validation_results)

        print(f"[Nutritionist] Summary: {len(approved_ids)} approved, {len(rejected_ids)} rejected")

        return state
