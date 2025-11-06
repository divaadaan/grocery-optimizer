from typing import TypedDict, List, Dict, Literal, Optional
from datetime import datetime

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

    # SousChef outputs
    generated_recipes: Dict[str, Recipe]
    sous_chef_assignments: Dict[str, str]

    # Nutritionist validation
    validation_results: Dict[str, ValidationResult]
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

    # MLflow tracking
    mlflow_run_id: str
    agent_call_log: List[Dict]

    # Workflow control
    status: Literal["initializing", "planning", "generating",
                   "validating", "retrying", "completed", "failed"]
    errors: List[str]
    warnings: List[str]
