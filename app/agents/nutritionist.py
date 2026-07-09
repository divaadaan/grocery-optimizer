from langchain_ollama import ChatOllama
import json
import time
from typing import Dict
from .state import RecipeGenerationState, ValidationResult
from .prompts import PromptTemplates
from .llm_output import NutritionistVerdict, invoke_validated
from ..config import settings
from ..services.mlflow_logger import MLflowLogger

class Nutritionist:
    """Nutritionist agent for recipe validation."""

    def __init__(self):
        self.llm = ChatOllama(
            model=settings.ollama_nutritionist_model,
            base_url=settings.ollama_base_url,
            temperature=0.3,  # Lower temperature for consistent validation
            format="json",
            client_kwargs={"timeout": settings.ollama_request_timeout}
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
                # Schema requires `approved`; other fields default. Invalid
                # output is retried with the error fed back to the model.
                verdict, _raw = invoke_validated(self.llm, prompt, NutritionistVerdict)

                result = ValidationResult(
                    recipe_id=recipe_id,
                    approved=verdict.approved,
                    feedback=verdict.feedback,
                    nutrition_facts=verdict.nutrition_facts,
                    dietary_compliance=verdict.dietary_compliance,
                    health_score=verdict.health_score
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
