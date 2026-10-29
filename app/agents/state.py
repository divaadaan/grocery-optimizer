import operator
from typing import List, Dict, Literal, Optional, Annotated

# pydantic (used by langgraph for schema introspection) requires the
# typing_extensions TypedDict on Python < 3.12
from typing_extensions import TypedDict


def merge_dicts(left: Dict, right: Dict) -> Dict:
    """Reducer for dict channels written by parallel nodes (e.g. 3 SousChefs)."""
    return {**left, **right}


class Recipe(TypedDict):
    """Individual recipe structure."""
    recipe_id: str
    name: str
    # [{name, deal_id | product_id, quantity, unit, price}]. Exactly one of
    # deal_id (an assigned deal) / product_id (a pantry staple) is set; name
    # is display-only. price is computed by costing.py from the citation.
    ingredients: List[Dict]
    instructions: List[str]
    servings: int
    total_cost: float
    estimated_prep_time: int
    meal_type: str
    cuisine_type: Optional[str]


class ValidationResult(TypedDict):
    """Nutritionist validation output."""
    recipe_id: str
    approved: bool
    feedback: str  # Specific improvement guidance if rejected
    nutrition_facts: Dict  # {calories, protein_g, carbs_g, fat_g, vitamins}
    dietary_compliance: Dict  # {allergen_free: bool, meets_restrictions: bool}
    health_score: float  # 0-100


class RecipeGenerationState(TypedDict):
    """Complete LangGraph state."""

    # Input configuration
    user_id: int
    postal_code: str
    # Spend limit for one budget period (users.budget), NOT for this one meal
    # plan. budget_period names the period; budget_spent_this_period is what
    # completed shopping lists have already consumed of it. See
    # app/services/budget.py.
    budget: float
    budget_period: str
    budget_spent_this_period: float
    household_size: int
    dietary_restrictions: List[str]
    num_meals: int
    preferences: Dict

    # Deal data from database
    available_deals: List[Dict]
    # str(deal_id) -> deal row. Keyed by id, never by product_name: the same
    # name at two stores is two deals.
    deal_index: Dict[str, Dict]

    # Curated product catalog for citations, loaded by load_recipe_catalog.
    # pantry_staples: confirmed products with assumed_on_hand (migration 014),
    #   with their attribute columns -- citable by product_id.
    # deal_products: str(deal_id) -> products linked through deal_products,
    #   with status + attributes. The Nutritionist's compliance verdict reads
    #   these and nothing else.
    pantry_staples: List[Dict]
    deal_products: Dict[str, List[Dict]]

    # Chef orchestration
    # One group per SousChef: 3, or num_meals when that is smaller, so no
    # SousChef is ever asked for 0 recipes (chef_orchestrator._merge_groups)
    ingredient_groups: List[List[Dict]]
    ingredient_reuse_map: Dict[str, int]
    target_ingredients_per_group: int

    # SousChef outputs — merged across parallel SousChef nodes; nodes return
    # only the *new* entries they produced
    generated_recipes: Annotated[Dict[str, Recipe], merge_dicts]
    sous_chef_assignments: Annotated[Dict[str, str], merge_dicts]

    # Nutritionist validation — validation_results accumulates across retry
    # iterations; the id lists are overwritten by their single sequential writer
    validation_results: Annotated[Dict[str, ValidationResult], merge_dicts]
    approved_recipe_ids: List[str]
    rejected_recipe_ids: List[str]

    # DB-assigned recipe ids for the saved approved recipes, aligned in order
    # with approved_recipe_ids — written once by finalize_meal_plan
    saved_recipe_ids: List[int]

    # Retry mechanism -- policy in ChefOrchestrator.handle_rejections:
    # iteration 1 "feedback" (same SousChef), iteration 2 "reassign_chef"
    # (the next SousChef's group). "none" until the first retry.
    iteration_count: int
    max_iterations: int
    retry_strategy: Literal["none", "feedback", "reassign_chef"]
    recipes_pending_retry: Dict[str, str]
    # Recipes still missing towards num_meals even if every rejected one is
    # fixed; retry_generation asks the SousChefs for this many new ones.
    recipes_shortfall: int

    # Cost tracking. total_cost is the plan's shopping-list cost: each cited
    # deal charged once however many recipes reuse it (costing.price_plan);
    # a recipe's own total_cost is informational. All costs are LOWER BOUNDS
    # (quantity, household size and multi-buy are ignored).
    # budget_remaining is what is left of the *period* budget after
    # already-completed spend and this plan:
    #   budget - budget_spent_this_period - total_cost
    # It was budget - total_cost, which handed every generation in the period
    # the whole period's money.
    total_cost: float
    cost_per_meal: float
    # None = no cited deal has a regular price (unknown, not zero)
    estimated_savings: Optional[float]
    budget_remaining: float
    # Post-generation budget check of the final plan (graph.plan_budget_check):
    # {status: "within_lower_bound" | "over", plan_cost, budget,
    #  spent_this_period, available, headroom, budget_period, lower_bound,
    #  caveats}. Written by finalize_meal_plan.
    budget_check: Dict

    # MLflow tracking — appended to by many nodes; nodes return only new entries
    mlflow_run_id: str
    agent_call_log: Annotated[List[Dict], operator.add]

    # Workflow control — append-only channels, nodes return only new entries
    status: Literal["initializing", "planning", "generating",
                    "validating", "retrying", "completed", "failed"]
    errors: Annotated[List[str], operator.add]
    warnings: Annotated[List[str], operator.add]
