from langchain_ollama import ChatOllama
import json
import time
from .state import RecipeGenerationState
from .prompts import PromptTemplates
from .llm_output import ChefPlan, LLMOutputError, invoke_validated, raw_output_report
from ..config import settings
from ..services.budget import available_for_plan
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger


def _deal_id(deal):
    return deal.get("deal_id", deal.get("id"))


def _restrict_to_served_deals(groups, served_deals):
    """Keep only selections that name a deal we actually served.

    Every deal in ``served_deals`` already passed the dietary gate in
    ``fetch_current_deals``, so membership in that set is the whole safety
    property here: a selection the model invented (a product not on the menu)
    is the only way an unsafe item could enter a group. A selection is matched
    by exact ``product_name`` first, then by ``deal_id``, and is rewritten to
    the served deal's own name and id so downstream lookups by name resolve.

    Groups emptied by the strip are backfilled from unused served deals so the
    downstream contract of 3 non-empty groups holds.
    Returns ``(cleaned_groups, num_removed)``.
    """
    by_name = {d.get("product_name"): d for d in served_deals}
    by_id = {str(_deal_id(d)): d for d in served_deals if _deal_id(d) is not None}

    used: set = set()
    cleaned: list = []
    removed = 0
    for group in groups:
        kept = []
        for sel in group:
            deal = by_name.get(sel.get("product_name"))
            if deal is None and sel.get("deal_id") is not None:
                deal = by_id.get(str(sel.get("deal_id")))
            if deal is None:
                removed += 1
                continue
            kept.append({
                **sel,
                "product_name": deal.get("product_name", ""),
                "deal_id": _deal_id(deal),
            })
            used.add(deal.get("product_name"))
        cleaned.append(kept)

    spare = [d for d in served_deals if d.get("product_name") not in used]
    for group in cleaned:
        if not group and spare:
            deal = spare.pop(0)
            used.add(deal.get("product_name"))
            group.append({
                "product_name": deal.get("product_name", ""),
                "quantity_estimate": "",
                "deal_id": _deal_id(deal),
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

        # Fetch deals from database.
        #
        # DIETARY GATE: the restrictions are passed so that a user with any
        # dietary restriction is served only deals linked to confirmed products
        # whose curated attributes clear those restrictions
        # (app/services/deal_gate.py). This is the only dietary filter on the
        # deal set; nothing downstream re-judges product names. Do not drop
        # this argument to "fetch more deals".
        deals = self.db.fetch_current_deals(
            state["postal_code"],
            dietary_restrictions=state["dietary_restrictions"],
        )

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
        # 2026-07-15). It is enforced upstream: available_deals were already
        # cleared against the user's restrictions by curated product
        # attributes in fetch_current_deals, so every deal shown here is safe.
        # The model is not trusted to stay on that menu, which is what
        # _restrict_to_served_deals below is for.
        served_deals = state["available_deals"]

        # Prepare prompt
        # The budget shown to the model is what is actually LEFT of the user's
        # period budget, not the whole period's money. state["budget"] is the
        # ceiling for a calendar week or month (app/services/budget.py);
        # planning against it after the user has already spent most of it is
        # how "within your spend limit" stopped being true. Floored at zero so
        # an overspent user's prompt never reads "Budget: $-12.40".
        spent = state.get("budget_spent_this_period") or 0.0
        budget_available = available_for_plan(state["budget"] - spent)
        if spent:
            print(f"[Chef] Planning against ${budget_available:.2f} of "
                  f"${state['budget']:.2f} (already spent ${spent:.2f} this "
                  f"{state.get('budget_period') or 'period'})")

        prompt = PromptTemplates.CHEF_INGREDIENT_PLANNING.format(
            deals_json=json.dumps(served_deals[:100], indent=2),  # Limit for context
            budget=round(budget_available, 2),
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

            # Safety net: a selection must be a deal we served. Strip anything
            # the model invented, backfilling emptied groups.
            ingredient_groups, removed = _restrict_to_served_deals(
                ingredient_groups, served_deals
            )
            if removed:
                print(f"[Chef] Removed {removed} item(s) not in the served deal set")

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
            if isinstance(e, LLMOutputError):
                print(f"[Chef] raw model output (never validated):\n{raw_output_report(e)}")
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
