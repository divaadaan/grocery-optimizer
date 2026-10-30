import json
import time
import uuid
from typing import Callable, Dict, List, Optional
from .state import Recipe
from .prompts import PromptTemplates, combination_rule_hints, hint_lines, preferences_text
from .llm_output import (
    GeneratedRecipe,
    LLMOutputError,
    RecipeBatch,
    citation_context,
    invoke_validated,
    raw_output_report,
)
from .costing import deals_by_id, ingredient_price, price_recipe
from .instruction_refs import render_step, structured_steps
from .llm_factory import make_llm, model_name
from ..services import products
from ..services.mlflow_logger import MLflowLogger


def offered_deals(
    ingredient_group: List[Dict],
    deal_lookup: Dict[str, Dict],
) -> List[Dict]:
    """The deals this SousChef may cite, resolved from the Chef's selections.

    Each selection is resolved to a real deal row by its ``deal_id`` **only**
    (``deal_lookup`` is keyed by ``str(deal_id)``). There is no
    ``product_name`` fallback: names collide across stores and never decide
    anything (CLAUDE.md). The prompt shows the deal row's own name, not the
    Chef's wording, because the citation -- not the name -- is what compliance
    and costing resolve. Unresolvable selections are dropped with a logged
    reason: nothing un-citable is offered.
    """
    out: List[Dict] = []
    seen = set()
    for sel in ingredient_group or []:
        deal = None
        if sel.get("deal_id") not in (None, ""):
            deal = deal_lookup.get(str(sel["deal_id"]).strip())
        if deal is None:
            reason = ("no deal_id" if sel.get("deal_id") in (None, "")
                      else "no such deal")
            print(f"[SousChef] dropped selection {sel.get('product_name')!r} "
                  f"(deal_id={sel.get('deal_id')!r}): {reason}")
            continue
        deal_id = deal.get("deal_id", deal.get("id"))
        if str(deal_id) in seen:
            continue
        seen.add(str(deal_id))
        out.append({
            "deal_id": deal_id,
            "product_name": deal.get("product_name", ""),
            "sale_price": deal.get("sale_price"),
            "quantity_estimate": sel.get("quantity_estimate", ""),
        })
    return out


def offered_staples(
    pantry_staples: List[Dict], dietary_restrictions: List[str]
) -> List[Dict]:
    """Pantry staples this SousChef may cite: only those whose curated
    attributes clear the user's restrictions, mirroring the deal gate. The
    Nutritionist still checks every citation; this keeps a vegetarian's
    SousChef from being offered fish sauce in the first place."""
    return [
        {"product_id": p["product_id"], "name": p.get("canonical_name", "")}
        for p in pantry_staples or []
        if p.get("status", "confirmed") == "confirmed"
        and products.product_allows(p, dietary_restrictions)
    ]


#: ``(chef_id, validated model output) -> recipe_id``. The id is the state key
#: for the recipe and is printed into the Nutritionist and retry prompts.
RecipeIdFactory = Callable[[str, GeneratedRecipe], str]


def random_recipe_id(chef_id: str, model: GeneratedRecipe) -> str:
    """Production recipe ids: a fresh uuid4 (arguments unused).

    The evaluation runner injects a content-derived factory instead
    (``app.eval.recorder.ContentRecipeIds``) so a replayed run reproduces the
    recorded prompts byte for byte."""
    return str(uuid.uuid4())


def cited_name(ingredient: Dict, deal_lookup: Dict[str, Dict],
               staple_names: Dict[str, str]) -> str:
    """The name of what an ingredient *cites*: the deal row's
    ``product_name`` or the offered staple's name. Falls back to the model's
    display ``name`` only if the citation does not resolve here (validation
    has already required the citation to be one this SousChef was offered)."""
    if ingredient.get("deal_id") is not None:
        deal = deal_lookup.get(str(ingredient["deal_id"])) or {}
        name = deal.get("product_name")
    else:
        name = staple_names.get(str(ingredient.get("product_id")))
    return name or ingredient.get("name", "")


def render_instructions(ingredients: List[Dict], templates: List[str],
                        deal_lookup: Dict[str, Dict],
                        staples: Optional[List[Dict]] = None) -> List[str]:
    """Render placeholder steps (``app/agents/instruction_refs.py``) to the
    text a user reads, substituting each ``{N}`` with the cited name of
    ``ingredients[N]``. The ingredient words come from cited,
    compliance-checked items; words the model typed literally do not (see
    the residual-risk note in ``instruction_refs``)."""
    staple_names = {str(s.get("product_id")): s.get("name", "") for s in staples or []}
    names = [cited_name(ing, deal_lookup, staple_names) for ing in ingredients]
    return [render_step(t, names.__getitem__) for t in templates]


def _to_recipe(
    chef_id: str, model: GeneratedRecipe, deal_lookup: Dict[str, Dict],
    recipe_id: RecipeIdFactory = random_recipe_id,
    staples: Optional[List[Dict]] = None,
) -> Recipe:
    """Build a Recipe from validated model output.

    Each ingredient keeps the one citation it carries; its ``price`` is
    overwritten from that citation (deal sale price, 0 for an on-hand staple)
    because model arithmetic is not trusted. total_cost is filled in by the
    caller from costing.price_recipe.

    ``instructions`` is the rendered text (what every string consumer reads);
    ``instruction_steps`` keeps the model's placeholder templates and their
    ingredient indices. ``staples`` are the staples offered to this call
    (``offered_staples``), for their names.
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
        print(f"[{chef_id}] Unresolved citations for {pricing.unpriced} — priced at 0")

    return Recipe(
        recipe_id=recipe_id(chef_id, model),
        name=model.name,
        ingredients=ingredients,
        instructions=render_instructions(ingredients, model.instructions,
                                         deal_lookup, staples),
        instruction_steps=structured_steps(model.instructions),
        servings=model.servings,
        total_cost=pricing.total_cost,
        estimated_prep_time=model.estimated_prep_time,
        meal_type=model.meal_type,
        cuisine_type=model.cuisine_type,
    )


def retry_view(recipe: Recipe) -> Dict:
    """The rejected recipe as the retry prompt shows it: instructions as the
    placeholder templates the model wrote, not the rendered text, so a
    revision reuses ``{N}`` references instead of copying ingredient names
    into the steps. Recipes without ``instruction_steps`` pass through."""
    steps = recipe.get("instruction_steps")
    view = {k: v for k, v in recipe.items() if k != "instruction_steps"}
    if steps:
        view["instructions"] = [s["template"] for s in steps]
    return view


class SousChef:
    """SousChef agent for recipe generation.

    ``llm`` (the "sous_chef" role), ``tracker`` (MLflowLogger's interface)
    and ``recipe_id`` (a :data:`RecipeIdFactory`) are injectable; left
    ``None`` they are the production defaults.
    """

    tracker = MLflowLogger
    recipe_id: RecipeIdFactory = staticmethod(random_recipe_id)

    def __init__(self, llm=None, tracker=None, recipe_id: Optional[RecipeIdFactory] = None):
        self.llm = llm if llm is not None else make_llm("sous_chef")
        if tracker is not None:
            self.tracker = tracker
        if recipe_id is not None:
            self.recipe_id = recipe_id

    @staticmethod
    def _offer(ingredient_group, available_deals, deal_index, pantry_staples,
               dietary_restrictions):
        """(deal_lookup, deals offered, staples offered, validation context).

        ``deal_index`` is the state's deal_id-keyed index (the same rows as
        ``available_deals``); it is rebuilt from ``available_deals`` when
        absent."""
        deal_lookup = deal_index or deals_by_id(available_deals)
        deals = offered_deals(ingredient_group, deal_lookup)
        staples = offered_staples(pantry_staples, dietary_restrictions)
        context = citation_context(
            [d["deal_id"] for d in deals], [s["product_id"] for s in staples]
        )
        return deal_lookup, deals, staples, context

    def generate_recipes(
        self,
        chef_id: str,
        ingredient_group: List[Dict],
        target_recipe_count: int,
        household_size: int,
        dietary_restrictions: List[str],
        available_deals: List[Dict],
        pantry_staples: List[Dict],
        deal_index: Optional[Dict[str, Dict]] = None,
        preferences: Optional[Dict] = None,
    ) -> List[Recipe]:
        """
        Generate recipes from assigned deals and on-hand staples.

        This is the parallel worker node. Every ingredient must cite an
        offered deal_id or staple product_id; a citation outside that set is a
        validation failure fed back to the model by invoke_validated.
        """
        print(f"[{chef_id}] Generating {target_recipe_count} recipes...")

        start_time = time.time()
        deal_lookup, deals, staples, context = self._offer(
            ingredient_group, available_deals, deal_index, pantry_staples,
            dietary_restrictions,
        )
        if not deals:
            print(f"[{chef_id}] No citable deals in this group — skipping generation")
            return []

        prompt = PromptTemplates.SOUS_CHEF_RECIPE_GENERATION.format(
            ingredients_json=json.dumps(deals, indent=2),
            pantry_json=json.dumps(staples, indent=2),
            target_recipe_count=target_recipe_count,
            servings=household_size,
            dietary_restrictions=", ".join(dietary_restrictions),
            combination_hints=hint_lines(combination_rule_hints(dietary_restrictions)),
            preferences=preferences_text(preferences),
        )

        try:
            batch, raw = invoke_validated(self.llm, prompt, RecipeBatch, context=context)
            recipes = [_to_recipe(chef_id, r, deal_lookup, self.recipe_id, staples)
                       for r in batch.recipes]

            duration = time.time() - start_time

            # Log to MLflow
            self.tracker.log_agent_call(
                agent_name=chef_id,
                tokens=len(raw),
                duration=duration,
                model=model_name("sous_chef"),
                success=True
            )

            print(f"[{chef_id}] Generated {len(recipes)} recipes in {duration:.2f}s")

            return recipes

        except Exception as e:
            print(f"[{chef_id}] ERROR: {e}")
            if isinstance(e, LLMOutputError):
                print(f"[{chef_id}] raw model output (never validated):\n"
                      f"{raw_output_report(e)}")
            return []

    def regenerate_with_feedback(
        self,
        chef_id: str,
        original_recipe: Recipe,
        feedback: str,
        ingredient_group: List[Dict],
        household_size: int,
        dietary_restrictions: List[str],
        available_deals: List[Dict],
        pantry_staples: List[Dict],
        deal_index: Optional[Dict[str, Dict]] = None,
        preferences: Optional[Dict] = None,
    ) -> Recipe:
        """
        Regenerate a rejected recipe with Nutritionist feedback.
        """
        print(f"[{chef_id}] Regenerating recipe with feedback...")

        deal_lookup, deals, staples, context = self._offer(
            ingredient_group, available_deals, deal_index, pantry_staples,
            dietary_restrictions,
        )
        if not deals:
            print(f"[{chef_id}] No citable deals in this group — cannot regenerate")
            return None

        prompt = PromptTemplates.SOUS_CHEF_RETRY_WITH_FEEDBACK.format(
            original_recipe_json=json.dumps(retry_view(original_recipe), indent=2),
            feedback=feedback,
            ingredients_json=json.dumps(deals, indent=2),
            pantry_json=json.dumps(staples, indent=2),
            servings=household_size,
            dietary_restrictions=", ".join(dietary_restrictions),
            combination_hints=hint_lines(combination_rule_hints(dietary_restrictions)),
            preferences=preferences_text(preferences),
        )

        try:
            batch, _raw = invoke_validated(self.llm, prompt, RecipeBatch, context=context)

            # Create new recipe with new ID
            recipe = _to_recipe(chef_id, batch.recipes[0], deal_lookup, self.recipe_id,
                                staples)

            print(f"[{chef_id}] Regenerated: {recipe['name']}")
            return recipe

        except Exception as e:
            print(f"[{chef_id}] Regeneration ERROR: {e}")
            if isinstance(e, LLMOutputError):
                print(f"[{chef_id}] raw model output (never validated):\n"
                      f"{raw_output_report(e)}")
            return None


def sous_chef_generate_node(task: dict, sous_chef: Optional[SousChef] = None) -> dict:
    """
    Node 4: Individual SousChef worker node for parallel execution.

    Invoked via Send() from the fan-out edge; `task` carries the worker's
    assignment (chef_id, ingredient_group, target_recipe_count) on top of the
    shared input fields. Returns only this worker's recipes — the merge_dicts
    reducers on generated_recipes/sous_chef_assignments combine the three
    parallel workers' outputs.

    The graph builder binds ``sous_chef`` (one instance shared by the three
    parallel tasks; it holds no per-call state). ``None`` builds a default.
    """
    chef_id = task.get("chef_id", "sous_chef_unknown")
    ingredient_group = task.get("ingredient_group", [])
    target_recipe_count = task.get("target_recipe_count", 2)

    if sous_chef is None:
        sous_chef = SousChef()

    recipes = sous_chef.generate_recipes(
        chef_id=chef_id,
        ingredient_group=ingredient_group,
        target_recipe_count=target_recipe_count,
        household_size=task["household_size"],
        dietary_restrictions=task["dietary_restrictions"],
        available_deals=task.get("available_deals", []),
        pantry_staples=task.get("pantry_staples", []),
        deal_index=task.get("deal_index", {}),
        preferences=task.get("preferences") or {},
    )

    return {
        "generated_recipes": {r["recipe_id"]: r for r in recipes},
        "sous_chef_assignments": {r["recipe_id"]: chef_id for r in recipes},
    }
