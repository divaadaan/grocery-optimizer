from langchain_ollama import ChatOllama
import json
import time
from typing import Dict, Any
from .state import RecipeGenerationState
from .prompts import PromptTemplates
from .llm_output import ChefPlan, invoke_validated
from ..config import settings
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger

class ChefOrchestrator:
    """Chef agent for high-level planning (largest configured model)."""

    def __init__(self):
        self.llm = ChatOllama(
            model=settings.ollama_chef_model,
            base_url=settings.ollama_base_url,
            temperature=0.7,
            format="json"
        )
        self.db = DatabaseService()

    def initialize(self, state: RecipeGenerationState) -> dict:
        """
        Node 1: Initialize Chef by fetching deals and setting up workflow.
        """
        print(f"[Chef] Initializing for user {state['user_id']}...")

        # Start MLflow run
        mlflow_run_id = MLflowLogger.start_run(
            user_id=state["user_id"],
            num_meals=state["num_meals"],
            budget=state["budget"],
            dietary_restrictions=state["dietary_restrictions"]
        )

        # Fetch deals from database
        deals = self.db.fetch_current_deals(state["postal_code"])

        # Create deal index for O(1) lookups
        deal_index = {deal["product_name"]: deal for deal in deals}

        print(f"[Chef] Found {len(deals)} deals for postal code {state['postal_code']}")

        return {
            "mlflow_run_id": mlflow_run_id,
            "available_deals": deals,
            "deal_index": deal_index,
            "status": "planning",
            "iteration_count": 0,
            "max_iterations": 2,
            "approved_recipe_ids": [],
            "rejected_recipe_ids": [],
        }

    def plan_ingredient_groups(self, state: RecipeGenerationState) -> dict:
        """
        Node 2: Use LLM to create 3 optimized ingredient groups.
        """
        print("[Chef] Planning ingredient groups for 3 SousChefs...")

        start_time = time.time()

        # Prepare prompt
        prompt = PromptTemplates.CHEF_INGREDIENT_PLANNING.format(
            deals_json=json.dumps(state["available_deals"][:100], indent=2),  # Limit for context
            budget=state["budget"],
            household_size=state["household_size"],
            num_meals=state["num_meals"],
            dietary_restrictions=", ".join(state["dietary_restrictions"]),
            preferences=json.dumps(state.get("preferences", {}))
        )

        # Call LLM — schema enforces exactly 3 non-empty groups, with retries
        # on invalid output (see llm_output.invoke_validated)
        try:
            plan, raw = invoke_validated(self.llm, prompt, ChefPlan)

            ingredient_groups = [
                [selection.model_dump() for selection in group]
                for group in plan.ingredient_groups
            ]
            ingredient_reuse_map = plan.ingredient_reuse_map

            # Log to MLflow
            duration = time.time() - start_time
            MLflowLogger.log_agent_call(
                agent_name="Chef_Orchestrator",
                tokens=len(raw),  # Approximate
                duration=duration,
                model=settings.ollama_chef_model,
                success=True
            )
            MLflowLogger.log_ingredient_groups(ingredient_groups, ingredient_reuse_map)

            print(f"[Chef] Created 3 ingredient groups with {len(ingredient_reuse_map)} unique ingredients")
            print(f"[Chef] Ingredient reuse: {ingredient_reuse_map}")

            return {
                "ingredient_groups": ingredient_groups,
                "ingredient_reuse_map": ingredient_reuse_map,
                "status": "generating",
                "agent_call_log": [{
                    "agent": "Chef_Orchestrator",
                    "action": "plan_ingredient_groups",
                    "timestamp": time.time(),
                    "duration": duration,
                    "success": True
                }],
            }

        except Exception as e:
            print(f"[Chef] ERROR: {e}")
            return {
                "errors": [f"Chef planning failed: {str(e)}"],
                "status": "failed",
            }

    def handle_rejections(self, state: RecipeGenerationState) -> dict:
        """
        Node 7: Process rejected recipes and determine retry strategy.
        """
        print(f"[Chef] Handling {len(state['rejected_recipe_ids'])} rejected recipes...")

        iteration_count = state["iteration_count"] + 1
        update = {"iteration_count": iteration_count}

        # Determine strategy
        if iteration_count == 1:
            # Strategy A: Reassign to different SousChef
            print("[Chef] Strategy: Reassign to different SousChef")

            # Keep same ingredient groups, just mark for retry
            update["retry_strategy"] = "reassign_chef"
            update["recipes_pending_retry"] = {
                recipe_id: state["validation_results"][recipe_id]["feedback"]
                for recipe_id in state["rejected_recipe_ids"]
            }

        else:
            # Strategy B: Select new ingredients
            print("[Chef] Strategy: Select new ingredients from remaining deals")

            # Use LLM to pick new ingredients
            # (Implementation similar to plan_ingredient_groups)
            # For now, simplified version
            update["retry_strategy"] = "new_ingredients"
            update["recipes_pending_retry"] = {}
            update["warnings"] = ["Max iterations reached, selecting new ingredients"]

        return update
