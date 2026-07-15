from langchain_ollama import ChatOllama
import json
import time
from typing import Dict
from .state import RecipeGenerationState, ValidationResult
from .prompts import PromptTemplates
from .llm_output import NutritionistVerdict, invoke_validated
from .dietary import compliance_report
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

            # Dietary compliance is decided deterministically in code — the LLM
            # verdict is ignored. Every model tried (SFT'd SmolLM2, qwen2.5:1.5b,
            # llama3.1:8b, mistral:7b) false-rejects compliant recipes and
            # confabulates violations regardless of prompt/base/SFT
            # (ROADMAP 2026-07-15); see app/agents/dietary.py.
            report = compliance_report(recipe, state["dietary_restrictions"])
            approved = report["approved"]
            violations = report["violations"]

            restrictions_desc = ", ".join(state["dietary_restrictions"]) or "no dietary"
            if approved:
                feedback = f"Approved: all ingredients comply with the {restrictions_desc} restriction."
            else:
                feedback = (
                    f"Rejected: {', '.join(violations)} violate the "
                    f"{restrictions_desc} restriction."
                )

            # The LLM is advisory only: it supplies nutrition facts + a rough
            # health estimate. Best-effort — a schema/parse failure must not
            # affect the (already-decided) compliance verdict.
            prompt = PromptTemplates.NUTRITIONIST_VALIDATION.format(
                recipe_json=json.dumps(recipe, indent=2),
                dietary_restrictions=", ".join(state["dietary_restrictions"])
            )
            nutrition_facts: Dict = {}
            health_score = 0.0
            try:
                verdict, _raw = invoke_validated(self.llm, prompt, NutritionistVerdict)
                nutrition_facts = verdict.nutrition_facts
                health_score = verdict.health_score
            except Exception as e:
                print(f"[Nutritionist] advisory nutrition facts unavailable for {recipe['name']}: {e}")
                errors.append(f"Nutrition-facts error for {recipe_id}: {str(e)}")

            result = ValidationResult(
                recipe_id=recipe_id,
                approved=approved,
                feedback=feedback,
                nutrition_facts=nutrition_facts,
                dietary_compliance={
                    "allergen_free": report["allergen_free"],
                    "meets_restrictions": report["meets_restrictions"],
                    "violations": violations,
                },
                health_score=health_score,
            )
            validation_results[recipe_id] = result

            if approved:
                approved_ids.append(recipe_id)
                print(f"[Nutritionist] ✓ APPROVED: {recipe['name']}")
            else:
                rejected_ids.append(recipe_id)
                print(f"[Nutritionist] ✗ REJECTED: {recipe['name']}  (violations: {', '.join(violations)})")

            duration = time.time() - start_time

            # Log to MLflow
            call_log.append({
                "agent": "Nutritionist",
                "action": "validate_recipe",
                "recipe_id": recipe_id,
                "approved": approved,
                "timestamp": time.time(),
                "duration": duration
            })

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
