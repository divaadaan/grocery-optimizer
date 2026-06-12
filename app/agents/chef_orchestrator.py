from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
import json
import time
from typing import Dict, Any
from .state import RecipeGenerationState
from .prompts import PromptTemplates
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

    def initialize(self, state: RecipeGenerationState) -> RecipeGenerationState:
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

        # Update state
        state["mlflow_run_id"] = mlflow_run_id
        state["available_deals"] = deals
        state["deal_index"] = deal_index
        state["status"] = "planning"
        state["iteration_count"] = 0
        state["max_iterations"] = 2
        state["agent_call_log"] = []
        state["errors"] = []
        state["warnings"] = []
        state["generated_recipes"] = {}
        state["validation_results"] = {}
        state["approved_recipe_ids"] = []
        state["rejected_recipe_ids"] = []

        print(f"[Chef] Found {len(deals)} deals for postal code {state['postal_code']}")

        return state

    def plan_ingredient_groups(self, state: RecipeGenerationState) -> RecipeGenerationState:
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

        # Call LLM
        try:
            response = self.llm.invoke([HumanMessage(content=prompt)])
            result = json.loads(response.content)

            # Extract groups
            ingredient_groups = result["ingredient_groups"]
            ingredient_reuse_map = result["ingredient_reuse_map"]

            # Validate we have 3 groups
            if len(ingredient_groups) != 3:
                raise ValueError(f"Expected 3 groups, got {len(ingredient_groups)}")

            # Update state
            state["ingredient_groups"] = ingredient_groups
            state["ingredient_reuse_map"] = ingredient_reuse_map
            state["status"] = "generating"

            # Log to MLflow
            duration = time.time() - start_time
            MLflowLogger.log_agent_call(
                agent_name="Chef_Orchestrator",
                tokens=len(response.content),  # Approximate
                duration=duration,
                model=settings.ollama_chef_model,
                success=True
            )
            MLflowLogger.log_ingredient_groups(ingredient_groups, ingredient_reuse_map)

            state["agent_call_log"].append({
                "agent": "Chef_Orchestrator",
                "action": "plan_ingredient_groups",
                "timestamp": time.time(),
                "duration": duration,
                "success": True
            })

            print(f"[Chef] Created 3 ingredient groups with {len(ingredient_reuse_map)} unique ingredients")
            print(f"[Chef] Ingredient reuse: {ingredient_reuse_map}")

        except Exception as e:
            state["errors"].append(f"Chef planning failed: {str(e)}")
            state["status"] = "failed"
            print(f"[Chef] ERROR: {e}")

        return state

    def handle_rejections(self, state: RecipeGenerationState) -> RecipeGenerationState:
        """
        Node 7: Process rejected recipes and determine retry strategy.
        """
        print(f"[Chef] Handling {len(state['rejected_recipe_ids'])} rejected recipes...")

        state["iteration_count"] += 1

        # Determine strategy
        if state["iteration_count"] == 1:
            # Strategy A: Reassign to different SousChef
            state["retry_strategy"] = "reassign_chef"
            print("[Chef] Strategy: Reassign to different SousChef")

            # Keep same ingredient groups, just mark for retry
            state["recipes_pending_retry"] = {
                recipe_id: state["validation_results"][recipe_id]["feedback"]
                for recipe_id in state["rejected_recipe_ids"]
            }

        else:
            # Strategy B: Select new ingredients
            state["retry_strategy"] = "new_ingredients"
            print("[Chef] Strategy: Select new ingredients from remaining deals")

            # Use LLM to pick new ingredients
            # (Implementation similar to plan_ingredient_groups)
            # For now, simplified version
            state["recipes_pending_retry"] = {}
            state["warnings"].append("Max iterations reached, selecting new ingredients")

        return state
