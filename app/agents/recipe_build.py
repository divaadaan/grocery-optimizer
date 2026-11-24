"""Turn a validated model recipe into the stored recipe.

Used by the per-dish SousChef (``app/agents/dish_writer.py``). Each
ingredient keeps the one citation it carries (a ``deal_id`` or a staple
``product_id``); its price comes from that citation, never from model
arithmetic (``costing``); instruction templates are rendered from the cited
names (``instruction_refs``). Nothing here decides dietary compliance --
that is ``dietary.py``, over the cited products' curated attributes.
"""
import logging
import uuid
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from .costing import ingredient_price, price_recipe
from .instruction_refs import deal_cited_name, render_step, structured_steps
from .llm_output import GeneratedRecipe
from .state import Recipe

logger = logging.getLogger(__name__)


#: ``(chef_id, validated model output) -> recipe_id``. The id keys the recipe
#: in the pipeline's result.
RecipeIdFactory = Callable[[str, GeneratedRecipe], str]


def random_recipe_id(chef_id: str, model: GeneratedRecipe) -> str:
    """Production recipe ids: a fresh uuid4 (arguments unused).

    The evaluation runner injects a content-derived factory instead
    (``app.eval.recorder.ContentRecipeIds``) so a replayed run reproduces the
    recorded prompts byte for byte."""
    return str(uuid.uuid4())


def cited_name(ingredient: Dict, deal_lookup: Dict[str, Dict],
               staple_names: Dict[str, str],
               deal_products: Optional[Mapping[str, Sequence[Dict]]] = None) -> str:
    """The name of what an ingredient *cites*: for a deal, its linked
    product's curated ``canonical_name`` (``deal_cited_name``: only when
    exactly one confirmed product is linked, else the deal row's flyer
    ``product_name``); for a staple, the offered staple's name. Falls back to the model's
    display ``name`` only if the citation does not resolve here (validation
    has already required the citation to be one this SousChef was offered)."""
    if ingredient.get("deal_id") is not None:
        deal = deal_lookup.get(str(ingredient["deal_id"])) or {}
        key = str(ingredient["deal_id"])
        name = deal_cited_name(deal.get("product_name") or "",
                               (deal_products or {}).get(key) or [])
    else:
        name = staple_names.get(str(ingredient.get("product_id")))
    return name or ingredient.get("name", "")


def render_instructions(ingredients: List[Dict], templates: List[str],
                        deal_lookup: Dict[str, Dict],
                        staples: Optional[List[Dict]] = None,
                        deal_products: Optional[Mapping[str, Sequence[Dict]]] = None,
                        ) -> List[str]:
    """Render placeholder steps (``app/agents/instruction_refs.py``) to the
    text a user reads, substituting each ``{N}`` with the cited name of
    ``ingredients[N]``. The ingredient words come from cited,
    compliance-checked items; words the model typed literally do not (see
    the residual-risk note in ``instruction_refs``)."""
    staple_names = {str(s.get("product_id")): s.get("name", "") for s in staples or []}
    names = [cited_name(ing, deal_lookup, staple_names, deal_products)
             for ing in ingredients]
    return [render_step(t, names.__getitem__) for t in templates]


def build_recipe(
    chef_id: str, model: GeneratedRecipe, deal_lookup: Dict[str, Dict],
    recipe_id: RecipeIdFactory = random_recipe_id,
    staples: Optional[List[Dict]] = None,
    deal_products: Optional[Mapping[str, Sequence[Dict]]] = None,
) -> Recipe:
    """Build a Recipe from validated model output.

    Each ingredient keeps the one citation it carries; its ``price`` is
    overwritten from that citation (deal sale price, 0 for an on-hand staple)
    because model arithmetic is not trusted. total_cost is filled in by the
    caller from costing.price_recipe.

    ``instructions`` is the rendered text (what every string consumer reads);
    ``instruction_steps`` keeps the model's placeholder templates and their
    ingredient indices. ``staples`` are the staples offered to this call
    (the confirmed pantry), for their names; ``deal_products`` (state's
    ``deal_id`` -> linked products) gives cited deals their canonical names.
    """
    ingredients = []
    for ing in model.ingredients:
        data = ing.model_dump()
        for key in ("deal_id", "product_id"):
            if data.get(key) is None:
                data.pop(key, None)
        data["price"] = ingredient_price(data, deal_lookup) or 0.0
        ingredients.append(data)

    pricing = price_recipe(ingredients, deal_lookup)
    if pricing.unpriced:
        logger.warning("[%s] unresolved citations %s priced at 0", chef_id, pricing.unpriced)

    return Recipe(
        recipe_id=recipe_id(chef_id, model),
        name=model.name,
        ingredients=ingredients,
        instructions=render_instructions(ingredients, model.instructions,
                                         deal_lookup, staples, deal_products),
        instruction_steps=structured_steps(model.instructions),
        servings=model.servings,
        total_cost=pricing.total_cost,
        estimated_prep_time=model.estimated_prep_time,
        meal_type=model.meal_type,
        cuisine_type=model.cuisine_type,
    )
