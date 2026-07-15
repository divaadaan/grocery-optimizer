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
    ingredients: List[Dict]  # [{name, quantity, unit, price, store_id}]
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
    budget: float
    household_size: int
    dietary_restrictions: List[str]
    num_meals: int
    preferences: Dict

    # Deal data from database
    available_deals: List[Dict]
    deal_index: Dict[str, Dict]

    # Chef orchestration
    ingredient_groups: List[List[Dict]]  # 3 groups for 3 SousChefs
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

    # Retry mechanism
    iteration_count: int
    max_iterations: int
    retry_strategy: Literal["reassign_chef", "new_ingredients"]
    recipes_pending_retry: Dict[str, str]

    # Cost tracking
    total_cost: float
    cost_per_meal: float
    estimated_savings: float
    budget_remaining: float

    # MLflow tracking — appended to by many nodes; nodes return only new entries
    mlflow_run_id: str
    agent_call_log: Annotated[List[Dict], operator.add]

    # Workflow control — append-only channels, nodes return only new entries
    status: Literal["initializing", "planning", "generating",
                   "validating", "retrying", "completed", "failed"]
    errors: Annotated[List[str], operator.add]
    warnings: Annotated[List[str], operator.add]
