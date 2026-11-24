"""The stored recipe shape the pipeline produces (``recipe_build.build_recipe``)."""
from typing import Dict, List, Optional

from typing_extensions import TypedDict


class Recipe(TypedDict):
    """Individual recipe structure."""
    recipe_id: str
    name: str
    # [{name, deal_id | product_id, quantity, unit, price}]. Exactly one of
    # deal_id (a deal) / product_id (a pantry staple) is set; name is
    # display-only (shown from the citation). price is computed by costing.py
    # from the citation.
    ingredients: List[Dict]
    # Rendered step text: each {N} placeholder replaced by the cited name of
    # ingredients[N] (recipe_build.render_instructions). What string consumers read.
    instructions: List[str]
    # [{template, ingredient_refs}]: the model's placeholder steps and the
    # ingredient indices each references (agents/instruction_refs.py).
    instruction_steps: List[Dict]
    servings: int
    total_cost: float
    estimated_prep_time: int
    meal_type: str
    cuisine_type: Optional[str]
