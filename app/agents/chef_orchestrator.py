import json
import time
from .state import RecipeGenerationState
from .prompts import PromptTemplates, combination_rule_hints, hint_lines
from .costing import deals_by_id
from .llm_factory import make_llm, model_name, provider_name
from .llm_usage import MeteredLLM
from .llm_output import ChefPlan, LLMOutputError, invoke_validated, raw_output_report
from ..services.budget import available_for_plan
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger


def _deal_id(deal):
    return deal.get("deal_id", deal.get("id"))


def _restrict_to_served_deals(groups, served_deals):
    """Keep only selections that cite a deal we actually served, by id.

    Every deal in ``served_deals`` already passed the dietary gate in
    ``fetch_current_deals``, so membership in that set is the whole safety
    property here: a selection the model invented (a product not on the menu)
    is the only way an unsafe item could enter a group.

    A selection resolves by its ``deal_id`` **only**. One without a deal_id,
    or with one we did not serve, is invalid output and is dropped with a
    logged reason -- never resolved by ``product_name``, which collides across
    stores ("an ingredient or deal name never decides anything", CLAUDE.md).
    A kept selection is rewritten to the served deal's own name (display only).

    Groups emptied by the strip are backfilled from unused served deals so the
    downstream contract of non-empty groups holds.
    Returns ``(cleaned_groups, num_removed)``.
    """
    by_id = {str(_deal_id(d)): d for d in served_deals if _deal_id(d) is not None}

    used: set = set()
    cleaned: list = []
    removed = 0
    for group in groups:
        kept = []
        for sel in group:
            raw_id = sel.get("deal_id")
            key = "" if raw_id is None else str(raw_id).strip()
            deal = by_id.get(key) if key else None
            if deal is None:
                removed += 1
                reason = "no deal_id" if not key else f"deal_id {raw_id!r} was not served"
                print(f"[Chef] dropped selection {sel.get('product_name')!r}: {reason}")
                continue
            kept.append({
                **sel,
                "product_name": deal.get("product_name", ""),
                "deal_id": _deal_id(deal),
            })
            used.add(key)
        cleaned.append(kept)

    spare = [d for d in served_deals
             if _deal_id(d) is not None and str(_deal_id(d)) not in used]
    for group in cleaned:
        if not group and spare:
            deal = spare.pop(0)
            used.add(str(_deal_id(deal)))
            group.append({
                "product_name": deal.get("product_name", ""),
                "quantity_estimate": "",
                "deal_id": _deal_id(deal),
            })
    return cleaned, removed


def _merge_groups(groups, num_meals):
    """Fold the Chef's groups into at most ``num_meals`` groups.

    One SousChef per group, and a SousChef must never get a target of zero
    recipes, so a plan for 1 or 2 meals runs 1 or 2 SousChefs. Rather than
    discard the Chef's other groups, group ``j`` is folded into group
    ``j % num_meals`` (duplicate deal_ids dropped), so every planned deal
    stays on offer. Three or more meals leave the groups untouched.
    """
    k = max(1, min(len(groups), num_meals))
    if k == len(groups):
        return groups
    merged = [[] for _ in range(k)]
    seen = [set() for _ in range(k)]
    for j, group in enumerate(groups):
        i = j % k
        for sel in group:
            key = str(sel.get("deal_id"))
            if key not in seen[i]:
                seen[i].add(key)
                merged[i].append(sel)
    return merged


class ChefOrchestrator:
    """Chef agent for high-level planning (largest configured model).

    ``llm`` (the "chef" role), ``db`` (anything with ``fetch_current_deals``)
    and ``tracker`` (MLflowLogger's interface) are injectable; left ``None``
    they are the production defaults.
    """

    tracker = MLflowLogger

    def __init__(self, llm=None, db=None, tracker=None):
        self.llm = llm if llm is not None else make_llm("chef")
        self.db = db if db is not None else DatabaseService()
        if tracker is not None:
            self.tracker = tracker

    def initialize(self, state: RecipeGenerationState) -> dict:
        """
        Node 1: Initialize Chef by fetching deals and setting up workflow.
        """
        print(f"[Chef] Initializing for user {state['user_id']}...")

        # Start MLflow run
        mlflow_run_id = self.tracker.start_run(
            user_id=state["user_id"],
            num_meals=state["num_meals"],
            budget=state["budget"],
            dietary_restrictions=state["dietary_restrictions"],
            generation_id=state.get("generation_id"),
            models=(state.get("provenance") or {}).get("models"),
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

        # Deal index for O(1) lookups, keyed by str(deal_id). It used to be
        # keyed by product_name, which collides across stores (two stores'
        # "BROCCOLI CROWNS" are two deals); names never resolve a deal.
        deal_index = deals_by_id(deals)

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
            combination_hints=hint_lines(
                combination_rule_hints(state["dietary_restrictions"], unit="group")),
            preferences=json.dumps(state.get("preferences", {}))
        )

        # Call LLM — schema enforces exactly 3 non-empty groups, with retries
        # on invalid output (see llm_output.invoke_validated)
        try:
            # Metered for MLflow: provider-reported tokens across retries
            llm = MeteredLLM(self.llm)
            plan, raw = invoke_validated(llm, prompt, ChefPlan)

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

            # Fewer than 3 meals -> fewer SousChefs, never one with target 0.
            ingredient_groups = _merge_groups(ingredient_groups, state["num_meals"])

            ingredient_reuse_map = plan.ingredient_reuse_map

            # Log to MLflow
            duration = time.time() - start_time
            self.tracker.log_agent_call(
                agent_name="Chef_Orchestrator",
                duration=duration,
                model=model_name("chef"),
                success=True,
                usage=llm.usage,
                provider=provider_name(),
            )
            self.tracker.log_ingredient_groups(ingredient_groups, ingredient_reuse_map)

            print(f"[Chef] Created {len(ingredient_groups)} ingredient groups with {len(ingredient_reuse_map)} unique ingredients")
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
        Node 7: Pick this iteration's retry strategy and queue the work.

        Retry policy (one strategy per iteration; ``max_iterations`` is 2):

        1. ``"feedback"`` -- each rejected recipe goes back to the SousChef
           that wrote it, on the same ingredient group, with the
           Nutritionist's feedback (which names the violating citations).
           The cheapest fix: usually one ingredient has to go.
        2. ``"reassign_chef"`` -- a recipe still rejected goes to a
           *different* SousChef, i.e. the next ingredient group round-robin,
           with the same feedback. If the group itself is the problem, a
           different set of deals is the fix. With a single group there is
           no other SousChef, and it degrades to ``"feedback"``.

        "reassign_chef" used to be iteration 1 and reused the same SousChef
        and group; Strategy B, "new_ingredients", only cleared the retry
        queue -- a no-op that spent an iteration. Each name now does what it
        says, and "new_ingredients" is gone rather than faked.

        Either strategy also tops up a shortfall: ``recipes_shortfall`` is how
        many recipes are missing even if every rejected one is fixed (a
        SousChef returned fewer than its target, or nothing). retry_generation
        asks the SousChefs for that many new recipes.
        """
        print(f"[Chef] Handling {len(state['rejected_recipe_ids'])} rejected recipes...")

        iteration_count = state["iteration_count"] + 1
        strategy = "feedback" if iteration_count == 1 else "reassign_chef"
        shortfall = max(0, state["num_meals"]
                        - len(state["approved_recipe_ids"])
                        - len(state["rejected_recipe_ids"]))
        print(f"[Chef] Strategy: {strategy} (shortfall: {shortfall})")

        return {
            "iteration_count": iteration_count,
            "retry_strategy": strategy,
            "recipes_pending_retry": {
                recipe_id: state["validation_results"][recipe_id]["feedback"]
                for recipe_id in state["rejected_recipe_ids"]
            },
            "recipes_shortfall": shortfall,
        }
