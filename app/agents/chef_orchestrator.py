from langchain_ollama import ChatOllama
import json
import time
from .state import RecipeGenerationState
from .prompts import PromptTemplates
from .llm_output import ChefPlan, invoke_validated
from .dietary import is_compliant
from ..config import settings
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger


def _enforce_group_compliance(groups, compliant_deals, restrictions):
    """Strip any selection that violates the restrictions (defence against the
    LLM emitting a forbidden/hallucinated product despite a pre-filtered menu),
    then backfill emptied groups from unused compliant deals so the downstream
    contract of 3 non-empty groups holds. Returns (cleaned_groups, num_removed).
    """
    used: set = set()
    cleaned: list = []
    removed = 0
    for group in groups:
        kept = []
        for sel in group:
            if is_compliant(sel.get("product_name", ""), restrictions):
                kept.append(sel)
                used.add(sel.get("product_name", ""))
            else:
                removed += 1
        cleaned.append(kept)

    spare = [d for d in compliant_deals if d.get("product_name") not in used]
    for group in cleaned:
        if not group and spare:
            deal = spare.pop(0)
            used.add(deal.get("product_name"))
            group.append({
                "product_name": deal.get("product_name", ""),
                "quantity_estimate": "",
                "deal_id": deal.get("deal_id", deal.get("id")),
            })
    return cleaned, removed


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

        # Dietary compliance is enforced in code, not left to the LLM (which
        # groups meat into vegetarian plans regardless of the prompt — ROADMAP
        # 2026-07-15). Show the model only compliant deals so a forbidden item
        # can't be selected in the first place.
        restrictions = state["dietary_restrictions"]
        compliant_deals = [
            d for d in state["available_deals"]
            if is_compliant(d.get("product_name", ""), restrictions)
        ]
        dropped = len(state["available_deals"]) - len(compliant_deals)
        if dropped:
            print(f"[Chef] Filtered out {dropped} deal(s) violating {restrictions}")

        # Prepare prompt
        prompt = PromptTemplates.CHEF_INGREDIENT_PLANNING.format(
            deals_json=json.dumps(compliant_deals[:100], indent=2),  # Limit for context
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

            # Safety net: strip any non-compliant item the model emitted anyway
            # (hallucinated or otherwise), backfilling emptied groups.
            ingredient_groups, removed = _enforce_group_compliance(
                ingredient_groups, compliant_deals, restrictions
            )
            if removed:
                print(f"[Chef] Removed {removed} non-compliant item(s) from LLM groups")

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
