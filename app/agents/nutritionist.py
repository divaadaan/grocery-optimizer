from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
import json
import time
from typing import Dict
from .state import RecipeGenerationState, ValidationResult
from .prompts import PromptTemplates
from ..config import settings
from ..services.mlflow_logger import MLflowLogger

class Nutritionist:
    """Nutritionist agent for recipe validation."""

    def __init__(self):
        self.llm = ChatOllama(
            model=settings.ollama_nutritionist_model,
            base_url=settings.ollama_base_url,
            temperature=0.3,  # Lower temperature for consistent validation
            format="json"
        )

    def validate_recipes(self, state: RecipeGenerationState) -> dict:
        """
        Node 6: Validate all generated recipes.
        """
        print(f"[Nutritionist] Validating {len(state['generated_recipes'])} recipes...")

        validation_results = {}
        approved_ids = []
        rejected_ids = []
        errors = []
        call_log = []

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

                # Create ValidationResult — default missing fields rather than
                # crashing on model output variance (approved defaults to False)
                result = ValidationResult(
                    recipe_id=recipe_id,
                    approved=bool(validation_data.get("approved", False)),
                    feedback=validation_data.get("feedback", ""),
                    nutrition_facts=validation_data.get("nutrition_facts", {}),
                    dietary_compliance=validation_data.get("dietary_compliance", {}),
                    health_score=validation_data.get("health_score", 0.0)
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
                call_log.append({
                    "agent": "Nutritionist",
                    "action": "validate_recipe",
                    "recipe_id": recipe_id,
                    "approved": result["approved"],
                    "timestamp": time.time(),
                    "duration": duration
                })

            except Exception as e:
                print(f"[Nutritionist] ERROR validating {recipe['name']}: {e}")
                errors.append(f"Validation error for {recipe_id}: {str(e)}")
                # Graceful degradation: mark as pending
                rejected_ids.append(recipe_id)

        # Log aggregate metrics
        MLflowLogger.log_validation_results(validation_results)

        print(f"[Nutritionist] Summary: {len(approved_ids)} approved, {len(rejected_ids)} rejected")

        # validation_results / agent_call_log / errors have reducers — return
        # only new entries. The id lists are plain channels — return full values.
        return {
            "status": "validating",
            "validation_results": validation_results,
            "approved_recipe_ids": state["approved_recipe_ids"] + approved_ids,
            "rejected_recipe_ids": state["rejected_recipe_ids"] + rejected_ids,
            "agent_call_log": call_log,
            "errors": errors,
        }
