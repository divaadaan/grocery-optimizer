import logging
from functools import partial
from itertools import combinations
from math import comb
from langgraph.graph import StateGraph, END
from langgraph.types import Send
from typing import Literal, List, Optional, Union
from .state import RecipeGenerationState
from .chef_orchestrator import ChefOrchestrator
from .costing import LOWER_BOUND_CAVEATS, RecipePricing, deals_by_id, price_plan
from .deps import GraphDeps
from .llm_errors import LLMInfrastructureError, describe
from .sous_chef import SousChef, infrastructure_update, sous_chef_generate_node
from .nutritionist import Nutritionist
from ..services import products, recipe_catalog
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger

logger = logging.getLogger(__name__)

# No module-level agents or DB service: everything external is built from a
# GraphDeps by create_recipe_generation_graph (app/agents/deps.py). Node
# functions take their dependency as an optional argument that defaults to
# production, so they remain callable with just a state.


def load_recipe_catalog(state: RecipeGenerationState, catalog=None) -> dict:
    """
    Node 1b: Load the curated products recipes cite, once per run.

    * pantry_staples -> confirmed ``assumed_on_hand`` products; offered to the
      SousChefs as citable product_ids (filtered by restriction there).
    * deal_products  -> for every available deal, its linked products and
      their attributes; the Nutritionist's compliance verdict reads these.

    The links are read for every user: besides the compliance verdict, the
    SousChef renders a cited deal as its linked product's canonical name
    (``instruction_refs.deal_cited_name``), restricted or not.

    A failed read degrades, it never fails open: no staples means recipes
    can only cite deals; no deal links means a restricted user's recipes are
    all rejected (an unresolvable citation is a violation).
    """
    if catalog is None:
        catalog = recipe_catalog
    update: dict = {"pantry_staples": [], "deal_products": {}}
    warnings = []
    try:
        update["pantry_staples"] = catalog.fetch_pantry_staples()
    except Exception as e:
        print(f"[Catalog] pantry staples unavailable: {e}")
        warnings.append(f"Pantry staples unavailable ({type(e).__name__}); "
                        "recipes may cite assigned deals only")

    deal_ids = [d.get("deal_id") for d in state.get("available_deals") or []]
    try:
        update["deal_products"] = catalog.fetch_deal_products(deal_ids)
    except Exception as e:
        print(f"[Catalog] deal product links unavailable: {e}")
        if products.restrictions_to_attributes(state["dietary_restrictions"]):
            warnings.append(f"Deal product links unavailable ({type(e).__name__}); "
                            "dietary compliance cannot be confirmed for any deal")
        else:
            warnings.append(f"Deal product links unavailable ({type(e).__name__}); "
                            "instructions will use flyer product names")

    print(f"[Catalog] {len(update['pantry_staples'])} pantry staples, "
          f"{len(update['deal_products'])} linked deals")
    if warnings:
        update["warnings"] = warnings
    return update


def budget_remaining_after(state: RecipeGenerationState, total_cost: float) -> float:
    """What is left of the user's *period* budget if this plan is bought.

    ``state["budget"]`` is the ceiling for one budget period (a calendar week
    or month — ``app/services/budget.py``), not for one generation, and
    ``budget_spent_this_period`` is what completed shopping lists have already
    taken out of it. This used to be ``budget - total_cost``, which gave every
    generation inside the period the whole period's money: four weekly plans
    in a month each believed they had the full monthly budget.

    Deliberately not floored at zero — an over-budget plan must report a
    negative number rather than a reassuring zero. (The figure handed to the
    *prompts* is floored; see ``budget.available_for_plan``.)

    ``.get`` with a 0.0 default so a state assembled without the new channel
    degrades to the old arithmetic instead of raising.
    """
    spent = state.get("budget_spent_this_period") or 0.0
    return state["budget"] - spent - total_cost


def _plan_pricing(state: RecipeGenerationState, recipes) -> RecipePricing:
    """The plan's shopping-list cost and savings (``costing.price_plan``).

    Cost semantics (owner decision, Track C): each cited deal is charged
    once, however many of ``recipes`` reuse it -- the plan is bought as one
    shopping list. A recipe's own ``total_cost`` stays on the recipe as an
    informational "bought alone" figure and is never summed. Staples cost 0.

    Still a LOWER BOUND: quantity, household size and multi-buy pricing are
    ignored (Track A4 pending).

    ``estimated_savings`` is ``None`` when no cited deal carries a regular
    price: unknown savings stay absent end to end (state, API ``null``,
    MLflow metric skipped) rather than becoming a fabricated 0.
    """
    return price_plan(recipes, deals_by_id(state.get("available_deals") or []))


def split_recipe_count(num_meals: int, num_chefs: int) -> List[int]:
    """Spread ``num_meals`` over at most ``num_chefs`` SousChefs, none at 0.

    7 over 3 -> [3, 2, 2]; 2 over 3 -> [1, 1] (only two SousChefs run).
    """
    k = max(0, min(num_meals, num_chefs))
    return [num_meals // k + (1 if i < num_meals % k else 0) for i in range(k)]


def fan_out_to_sous_chefs(
    state: RecipeGenerationState,
) -> Union[List[Send], Literal["handle_failure"]]:
    """
    Conditional edge after planning: fan-out to one SousChef per group.

    Send() only works from a conditional edge — each Send dispatches one
    sous_chef_generate task with that worker's assignment.

    The Chef plans 3 groups but folds them into ``num_meals`` groups when
    fewer meals are asked for (``chef_orchestrator._merge_groups``), so 1 or
    2 meals run 1 or 2 SousChefs and no task ever has a target of 0. The
    targets sum to exactly ``num_meals``.
    """
    groups = state["ingredient_groups"]
    if state["status"] == "failed" or not groups:
        return "handle_failure"

    targets = split_recipe_count(state["num_meals"], len(groups))
    print(f"[Orchestrator] Distributing {state['num_meals']} recipes to "
          f"{len(targets)} SousChef(s): {targets}")

    return [
        Send("sous_chef_generate", {
            "household_size": state["household_size"],
            "dietary_restrictions": state["dietary_restrictions"],
            "preferences": state.get("preferences") or {},
            "deal_index": state["deal_index"],
            "available_deals": state.get("available_deals") or [],
            "pantry_staples": state.get("pantry_staples") or [],
            "deal_products": state.get("deal_products") or {},
            "chef_id": f"sous_chef_{i+1}",
            "ingredient_group": groups[i],
            "target_recipe_count": target,
        })
        for i, target in enumerate(targets)
    ]


def aggregate_recipes(state: RecipeGenerationState) -> dict:
    """
    Node 5: Aggregate results from parallel SousChefs (fan-in).
    """
    print(f"[Orchestrator] Aggregating {len(state['generated_recipes'])} recipes...")

    # Interim figures over everything generated so far. Costs come from the
    # ingredient citations (costing) — model arithmetic is not trusted
    # anywhere — and each cited deal is charged once across the plan.
    pricing = _plan_pricing(state, state["generated_recipes"].values())
    total_cost = pricing.total_cost
    estimated_savings = pricing.estimated_savings
    num_recipes = len(state["generated_recipes"])
    budget_remaining = budget_remaining_after(state, total_cost)

    spent = state.get("budget_spent_this_period") or 0.0
    print(f"[Orchestrator] Total cost: ${total_cost:.2f}, already spent this "
          f"{state.get('budget_period') or 'period'}: ${spent:.2f}, "
          f"budget remaining: ${budget_remaining:.2f}")

    return {
        "total_cost": total_cost,
        "cost_per_meal": total_cost / num_recipes if num_recipes else 0,
        "budget_remaining": budget_remaining,
        "estimated_savings": estimated_savings,
    }


def route_after_validation(
    state: RecipeGenerationState,
) -> Literal["finalize", "retry", "finalize_partial", "handle_failure"]:
    """
    Conditional edge: Decide next step after Nutritionist validation.
    Pure routing — must not write state.

    The recipe count is enforced: the plan finalizes as soon as it has
    ``num_meals`` approved recipes (surplus ones are trimmed in
    finalize_meal_plan). Short of that, it retries while iterations remain —
    for rejected recipes *and* for a shortfall (a SousChef that returned too
    few or none) — and only then settles for a partial plan (>= 60% of the
    request) or fails.

    Infrastructure failures short-circuit the retries (owner decision
    2026-11-23): once any agent recorded one in ``infrastructure_errors``
    (``llm_errors.classify``: model unreachable, refused, timed out, HTTP
    5xx) the run settles immediately -- finalize_partial at >= 60%,
    otherwise handle_failure -- even with iterations left. Every SousChef
    shares one model server, so a retry would hit the same dead model.
    Model-output failures (bad JSON / schema / citations) keep retrying.
    """
    num_rejected = len(state["rejected_recipe_ids"])
    num_approved = len(state["approved_recipe_ids"])
    num_requested = state["num_meals"]

    print(f"[Router] Approved: {num_approved}, Rejected: {num_rejected}, "
          f"Iteration: {state['iteration_count']}")

    if num_approved >= num_requested:
        return "finalize"

    infra = state.get("infrastructure_errors") or []
    if infra:
        print(f"[Router] Infrastructure failure ({describe(infra[0])}) - "
              "not retrying")
    elif state["iteration_count"] < state["max_iterations"]:
        print(f"[Router] {num_approved}/{num_requested} approved - retrying")
        return "retry"

    # Out of iterations, or the model is unreachable. Nothing approved:
    # finalizing would falsely report success.
    if num_approved > 0 and num_approved >= num_requested * 0.6:
        print("[Router] Partial success - finalizing with approved recipes")
        return "finalize_partial"
    print("[Router] Insufficient approved recipes - failing")
    return "handle_failure"


def retry_generation(state: RecipeGenerationState,
                     sous_chef: Optional[SousChef] = None) -> dict:
    """
    Node 8: Regenerate rejected recipes with feedback, and top up a shortfall.

    Which SousChef (ingredient group) gets a rejected recipe depends on
    ``retry_strategy`` (policy: ``ChefOrchestrator.handle_rejections``):
    ``"feedback"`` keeps the original SousChef; ``"reassign_chef"`` hands it
    to the next SousChef round-robin. ``recipes_shortfall`` new recipes are
    then requested, spread over the groups like the initial fan-out.
    """
    print(f"[Orchestrator] Retrying {len(state['recipes_pending_retry'])} recipes "
          f"({state.get('retry_strategy')}), shortfall "
          f"{state.get('recipes_shortfall') or 0}...")

    if sous_chef is None:
        sous_chef = SousChef()

    groups = state["ingredient_groups"]
    common = dict(
        household_size=state["household_size"],
        dietary_restrictions=state["dietary_restrictions"],
        available_deals=state.get("available_deals") or [],
        pantry_staples=state.get("pantry_staples") or [],
        deal_index=state["deal_index"],
        preferences=state.get("preferences") or {},
        deal_products=state.get("deal_products") or {},
    )
    reassign = state.get("retry_strategy") == "reassign_chef" and len(groups) > 1

    new_recipes = {}
    new_assignments = {}
    regenerated_ids = set()
    infra_writes: dict = {}

    # An unreachable model stops the pass at the first failure: the remaining
    # calls would go to the same dead server. What was produced before it is
    # kept, and route_after_validation stops retrying.
    try:
        for recipe_id, feedback in state["recipes_pending_retry"].items():
            original_recipe = state["generated_recipes"][recipe_id]

            original_chef_id = state["sous_chef_assignments"][recipe_id]
            chef_index = (int(original_chef_id.split("_")[-1]) - 1) % len(groups)
            if reassign:
                chef_index = (chef_index + 1) % len(groups)
            chef_id = f"sous_chef_{chef_index + 1}"

            new_recipe = sous_chef.regenerate_with_feedback(
                chef_id=chef_id,
                original_recipe=original_recipe,
                feedback=feedback,
                ingredient_group=groups[chef_index],
                **common,
            )

            if new_recipe:
                new_recipes[new_recipe["recipe_id"]] = new_recipe
                new_assignments[new_recipe["recipe_id"]] = chef_id
                regenerated_ids.add(recipe_id)

        shortfall = state.get("recipes_shortfall") or 0
        for i, target in enumerate(split_recipe_count(shortfall, len(groups))):
            chef_id = f"sous_chef_{i + 1}"
            for recipe in sous_chef.generate_recipes(
                chef_id=chef_id,
                ingredient_group=groups[i],
                target_recipe_count=target,
                **common,
            ):
                new_recipes[recipe["recipe_id"]] = recipe
                new_assignments[recipe["recipe_id"]] = chef_id
    except LLMInfrastructureError as e:
        infra_writes = infrastructure_update(e)

    return {
        **infra_writes,
        "generated_recipes": new_recipes,
        "sous_chef_assignments": new_assignments,
        # Regenerated recipes get fresh IDs and a fresh validation pass;
        # drop their old IDs from the rejected list
        "rejected_recipe_ids": [
            rid for rid in state["rejected_recipe_ids"] if rid not in regenerated_ids
        ],
        "recipes_pending_retry": {},
        "recipes_shortfall": 0,
    }


#: Above this many candidate subsets, select_plan keeps approval order.
_MAX_PLAN_SUBSETS = 5000


def select_plan(approved_ids: List[str], recipes: dict, num_meals: int,
                deal_lookup: dict) -> List[str]:
    """The approved recipes that make up the plan: exactly ``num_meals`` of
    them when more were approved (a SousChef may return extras).

    Deterministic and budget-aware without ever dropping a requested meal:
    among the ``num_meals``-sized subsets it keeps the one with the lowest
    shopping-list cost (``costing.price_plan``), ties going to approval
    order. When the subsets are too many to enumerate it keeps the first
    ``num_meals`` in approval order.
    """
    if len(approved_ids) <= num_meals:
        return list(approved_ids)
    if comb(len(approved_ids), num_meals) > _MAX_PLAN_SUBSETS:
        return list(approved_ids[:num_meals])
    best = min(
        combinations(approved_ids, num_meals),
        key=lambda ids: price_plan([recipes[r] for r in ids], deal_lookup).total_cost,
    )
    return list(best)


#: Below this, a plan cost counts as equal to the money available.
_COST_EPS = 0.005


def plan_budget_check(state: RecipeGenerationState, plan_cost: float) -> dict:
    """Check the final plan's cost against what is left of the period budget.

    Behaviour when over (design): the plan is still returned and the job
    still completes, with ``status: "over"`` here and a warning. Dropping
    meals to fit would trade away the requested plan silently, and an LLM
    retry for "cheaper" is neither deterministic nor guaranteed; the Chef is
    already planned against the remaining budget, and surplus approved
    recipes are trimmed to the cheapest subset (``select_plan``). What must
    never happen is a fabricated "within budget": the cost is a lower bound,
    so the best this check can say is ``"within_lower_bound"`` -- the plan
    may still cost more at the till.
    """
    budget = float(state["budget"])
    spent = float(state.get("budget_spent_this_period") or 0.0)
    available = budget - spent
    headroom = available - plan_cost
    return {
        "status": "over" if headroom < -_COST_EPS else "within_lower_bound",
        "plan_cost": round(plan_cost, 2),
        "budget": round(budget, 2),
        "spent_this_period": round(spent, 2),
        "available": round(available, 2),
        "headroom": round(headroom, 2),
        "budget_period": state.get("budget_period"),
        "lower_bound": True,
        "caveats": list(LOWER_BOUND_CAVEATS),
    }


def _persistable(state: RecipeGenerationState, recipe_id: str) -> dict:
    """An approved recipe as the store persists it: the generated recipe
    (citations and ``instruction_steps`` untouched) plus the Nutritionist's advisory nutrition facts,
    health score and compliance verdict, which live in validation_results.
    A copy — state's recipe is not modified."""
    validation = (state.get("validation_results") or {}).get(recipe_id) or {}
    return {
        **state["generated_recipes"][recipe_id],
        "nutrition_facts": validation.get("nutrition_facts"),
        "health_score": validation.get("health_score"),
        "dietary_compliance": validation.get("dietary_compliance"),
    }


def _generation_record(state: RecipeGenerationState, num_saved: int,
                       total_cost: Optional[float], estimated_savings: Optional[float],
                       failure_reason: Optional[str] = None) -> dict:
    """Provenance of this generation (the ``recipe_generations`` row): what
    the plan was generated against and by what, enough to re-score it.

    ``failure_reason`` marks a failed run (``handle_failure``): status
    ``failed``, nothing saved, no plan totals. The run counts (iterations,
    generated / approved / rejected) are recorded for every outcome
    (migration 023)."""
    provenance = state.get("provenance") or {}
    if failure_reason is not None:
        status = "failed"
    elif num_saved >= state["num_meals"]:
        status = "completed"
    else:
        status = "partial"
    return {
        "generation_id": state.get("generation_id"),
        "status": status,
        "failure_reason": failure_reason,
        "num_meals_requested": state["num_meals"],
        "num_recipes_saved": num_saved,
        "iteration_count": state.get("iteration_count") or 0,
        "num_recipes_generated": len(state.get("generated_recipes") or {}),
        "num_recipes_approved": len(state.get("approved_recipe_ids") or []),
        "num_recipes_rejected": len(state.get("rejected_recipe_ids") or []),
        "dietary_restrictions": list(state["dietary_restrictions"]),
        "budget": state["budget"],
        "budget_period": state.get("budget_period"),
        "budget_spent_this_period": state.get("budget_spent_this_period"),
        "household_size": state["household_size"],
        "postal_code": state["postal_code"],
        "preferences": state.get("preferences") or {},
        "models": provenance.get("models") or {},
        "snapshot": provenance.get("snapshot"),
        "mlflow_run_id": state.get("mlflow_run_id") or None,
        "total_cost": total_cost,
        "estimated_savings": estimated_savings,
    }


def finalize_meal_plan(state: RecipeGenerationState, recipe_store=None,
                       tracker=MLflowLogger) -> dict:
    """
    Node 9: Finalize and save approved recipes.

    ``recipe_store`` is the graph's only write (``GraphDeps.recipe_store``);
    ``None`` means the production database.
    """
    print("[Orchestrator] Finalizing meal plan...")

    if recipe_store is None:
        recipe_store = DatabaseService()

    # Exactly num_meals recipes when more were approved (cheapest subset).
    deal_lookup = deals_by_id(state.get("available_deals") or [])
    plan_ids = select_plan(state["approved_recipe_ids"], state["generated_recipes"],
                           state["num_meals"], deal_lookup)
    approved_recipes = [state["generated_recipes"][rid] for rid in plan_ids]

    # Final metrics cover the meal plan only — aggregate's interim numbers
    # include recipes that were later rejected, regenerated or trimmed.
    # Shopping-list cost: each cited deal once (a lower bound).
    pricing = _plan_pricing(state, approved_recipes)
    total_cost = pricing.total_cost
    estimated_savings = pricing.estimated_savings
    num_approved = len(approved_recipes)
    budget_check = plan_budget_check(state, total_cost)

    # Save to database: the recipes plus their generation's provenance, so a
    # persisted plan can be re-scored later (migration 022)
    recipe_ids = recipe_store.save_recipes(
        state["user_id"],
        [_persistable(state, rid) for rid in plan_ids],
        generation=_generation_record(state, num_approved, total_cost,
                                      estimated_savings),
    )

    print(f"[Orchestrator] Saved {len(recipe_ids)} recipes to database")

    # Log final metrics to MLflow
    tracker.log_final_metrics(
        total_cost=total_cost,
        cost_per_meal=total_cost / num_approved if num_approved else 0,
        estimated_savings=estimated_savings,
        iterations=state["iteration_count"],
        recipe_count=num_approved,
        success=True
    )

    update = {
        "status": "completed",
        "total_cost": total_cost,
        "cost_per_meal": total_cost / num_approved if num_approved else 0,
        "estimated_savings": estimated_savings,
        "budget_remaining": budget_remaining_after(state, total_cost),
        "budget_check": budget_check,
        # The plan, aligned in order with saved_recipe_ids.
        "approved_recipe_ids": plan_ids,
        "saved_recipe_ids": recipe_ids,
    }

    warnings = []
    if num_approved < state["num_meals"]:
        warnings.append(f"Only {num_approved}/{state['num_meals']} recipes approved")
    if budget_check["status"] == "over":
        period = state.get("budget_period") or "period"
        warnings.append(
            f"Over budget: this plan costs at least ${total_cost:.2f} (a lower "
            f"bound), more than the ${budget_check['available']:.2f} left of "
            f"the {period} budget")
    if warnings:
        update["warnings"] = warnings

    # Finalize MLflow run
    tracker.finalize_run({**state, **update})

    print(f"[Orchestrator] ✓ Meal plan complete! {len(approved_recipes)} recipes ready.")

    return update


def failure_reason(state: RecipeGenerationState) -> str:
    """Why the run reached ``handle_failure``, for ``recipe_generations``.

    Two routes lead there (``fan_out_to_sous_chefs`` and
    ``route_after_validation``): planning produced no ingredient groups, or
    the retries ran out short of even a partial plan. Either can be caused
    by an unreachable model (``infrastructure_errors``); that is reported
    first and by name, so the row says the model failed, not the recipes."""
    infra = state.get("infrastructure_errors") or []
    if infra:
        first = describe(infra[0])
        others = sorted({describe(r) for r in infra[1:]} - {first})
        also = f"; also {', '.join(others)}" if others else ""
        return (f"Infrastructure failure: {first}{also} -- "
                f"{len(state.get('approved_recipe_ids') or [])}/"
                f"{state['num_meals']} approved after "
                f"{state.get('iteration_count') or 0} retry iteration(s) "
                f"(max {state.get('max_iterations')})")
    if state.get("status") == "failed" or not state.get("ingredient_groups"):
        detail = "; ".join(state.get("errors") or []) or "no ingredient groups"
        return f"Planning failed: {detail}"
    return (f"Insufficient approved recipes: "
            f"{len(state.get('approved_recipe_ids') or [])}/{state['num_meals']} "
            f"approved after {state.get('iteration_count') or 0} retry "
            f"iteration(s) (max {state.get('max_iterations')}), "
            f"{len(state.get('rejected_recipe_ids') or [])} still rejected")


def handle_failure(state: RecipeGenerationState, recipe_store=None,
                   tracker=MLflowLogger) -> dict:
    """
    Node 10: Handle workflow failure gracefully.

    Persists a ``status='failed'`` ``recipe_generations`` row (migration
    023): the same provenance ``finalize_meal_plan`` writes, the failure
    reason and the run counts, and zero recipes. ``recipe_store`` is
    ``GraphDeps.recipe_store``; ``None`` means the production database.

    Recording the failure is best effort: if the write itself fails it is
    logged and reported as a warning, and the run still ends as the failure
    it is -- a broken audit write must not mask the original failure.
    """
    print("[Orchestrator] ✗ Workflow failed - graceful degradation")

    if recipe_store is None:
        recipe_store = DatabaseService()

    reason = failure_reason(state)
    update = {
        "status": "failed",
        "errors": ["Unable to generate sufficient approved recipes"],
    }
    if state.get("infrastructure_errors"):
        update["errors"].append(reason)
    try:
        recipe_store.save_recipes(
            state["user_id"], [],
            generation=_generation_record(state, 0, None, None,
                                          failure_reason=reason),
        )
    except Exception as e:  # noqa: BLE001 - audit write must not mask the failure
        logger.error("Could not record failed generation %s: %s",
                     state.get("generation_id"), e, exc_info=True)
        update["warnings"] = [
            f"The failed generation could not be recorded ({type(e).__name__})"]

    # Log failure to MLflow
    tracker.log_final_metrics(
        total_cost=state["total_cost"],
        cost_per_meal=state["cost_per_meal"],
        estimated_savings=state["estimated_savings"],
        iterations=state["iteration_count"],
        recipe_count=len(state["approved_recipe_ids"]),
        success=False
    )

    tracker.finalize_run(state)

    return update


# Build the graph
def create_recipe_generation_graph(deps: Optional[GraphDeps] = None):
    """
    Construct the complete LangGraph workflow.

    ``deps`` supplies the LLMs, the deal/catalog reads, the recipe store and
    the tracker; ``None`` is ``GraphDeps()``, i.e. production.
    """
    if deps is None:
        deps = GraphDeps()

    chef = ChefOrchestrator(llm=deps.chef_llm, db=deps.deals, tracker=deps.tracker)
    sous_chef = SousChef(llm=deps.sous_chef_llm, tracker=deps.tracker,
                         recipe_id=deps.recipe_ids)
    nutritionist = Nutritionist(llm=deps.nutritionist_llm, tracker=deps.tracker)

    workflow = StateGraph(RecipeGenerationState)

    # Add nodes
    workflow.add_node("initialize_chef", chef.initialize)
    workflow.add_node("load_recipe_catalog",
                      partial(load_recipe_catalog, catalog=deps.catalog))
    workflow.add_node("plan_ingredient_groups", chef.plan_ingredient_groups)
    workflow.add_node("sous_chef_generate",
                      partial(sous_chef_generate_node, sous_chef=sous_chef))
    workflow.add_node("aggregate_recipes", aggregate_recipes)
    workflow.add_node("nutritionist_validate", nutritionist.validate_recipes)
    workflow.add_node("handle_rejections", chef.handle_rejections)
    workflow.add_node("retry_generation", partial(retry_generation, sous_chef=sous_chef))
    workflow.add_node("finalize_meal_plan",
                      partial(finalize_meal_plan, recipe_store=deps.recipe_store,
                              tracker=deps.tracker))
    workflow.add_node("handle_failure",
                      partial(handle_failure, recipe_store=deps.recipe_store,
                              tracker=deps.tracker))

    # Define edges
    workflow.set_entry_point("initialize_chef")
    workflow.add_edge("initialize_chef", "load_recipe_catalog")
    workflow.add_edge("load_recipe_catalog", "plan_ingredient_groups")

    # Fan-out: Send() dispatches 3 parallel sous_chef_generate tasks
    workflow.add_conditional_edges("plan_ingredient_groups", fan_out_to_sous_chefs)

    # Fan-in: aggregate runs once after all parallel tasks complete
    workflow.add_edge("sous_chef_generate", "aggregate_recipes")
    workflow.add_edge("aggregate_recipes", "nutritionist_validate")

    # Conditional routing after validation
    workflow.add_conditional_edges(
        "nutritionist_validate",
        route_after_validation,
        {
            "finalize": "finalize_meal_plan",
            "retry": "handle_rejections",
            "finalize_partial": "finalize_meal_plan",
            "handle_failure": "handle_failure"
        }
    )

    # Retry loop
    workflow.add_edge("handle_rejections", "retry_generation")
    workflow.add_edge("retry_generation", "nutritionist_validate")

    # Terminal nodes
    workflow.add_edge("finalize_meal_plan", END)
    workflow.add_edge("handle_failure", END)

    return workflow.compile()
