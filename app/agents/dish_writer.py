"""Write one recipe for one planned dish (the per-dish SousChef).

Approach C (``experiments/chef/DESIGN_APPROACH_C.md`` §7). The SousChef gets
only that dish's deals, the stock-up deals and the confirmed pantry -- "this
dish's ingredients", not the old "use ONLY the assigned deals" pressure that
forced tea into a chicken dinner. A deal is never forced in: an unused side is
fine.

The output contract is the existing recipe schema
(``app/agents/llm_output.GeneratedRecipe``): every ingredient cites a
``deal_id`` or a staple ``product_id`` from what this call was shown
(``citation_context``), and instructions write ingredients as ``{N}``
placeholders. Validation failures go back to the model once. Building the
stored recipe (prices from citations, rendered steps) reuses
``sous_chef._to_recipe``.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.agents import llm_chat
from app.agents.costing import deals_by_id
from app.agents.dish_planner import PlannedDish
from app.agents.llm_output import GeneratedRecipe, RecipeBatch, citation_context
from app.agents.prompts import combination_rule_hints, hint_lines, preferences_text
from app.agents.state import Recipe
from app.agents.sous_chef import RecipeIdFactory, _to_recipe, random_recipe_id

MAX_TOKENS = 2048
TEMPERATURE = 0.7

PROMPT = """You are a SousChef writing one practical, delicious home recipe.

**The dish:** {dish_name}
{why}

**This dish's ingredients bought this week (cite each by its deal_id):**
{dish_deals_json}

**Stock-up items also bought this week (optional; cite by deal_id):**
{stock_up_json}

**Pantry staples already on hand (cite each by its product_id):**
{pantry_json}

**Requirements:**
- Servings: {servings}
- Dietary restrictions: {dietary_restrictions}{combination_hints}
- Preferences: {preferences}
- Build the recipe around the dish's main ingredient. Use the listed ingredients that suit the dish;
  you do not have to use all of them.
- EVERY ingredient must cite exactly one source: "deal_id" for a deal above, OR "product_id" for a
  pantry staple. Copy the id exactly. If an ingredient is not listed, leave it out.
- "name" is the plain ingredient name a cook would write (use the deal's "item" when given, e.g.
  "chicken thighs", not the flyer text); costs are computed for you.
- Oil, salt, spices and other pantry staples you use are ingredients too: list each one with its
  product_id and refer to it by placeholder, never by typing it in a step.
{feedback}
**Instruction format:**
- Write EVERY ingredient in "instructions" as a placeholder {{N}}, where N is that ingredient's
  0-based position in "ingredients" ({{0}} is the first ingredient, {{1}} the second, ...).
- Never type an ingredient or food name in a step; use its placeholder.
- Use no other curly braces in the steps. Every ingredient should appear in at least one step.

**Output format (one JSON object, no comments, no code fences):**
{{"name": "Recipe Name", "meal_type": "dinner", "cuisine_type": "Italian", "ingredients": [{{"name": "chicken thighs", "deal_id": 123, "quantity": "1.5", "unit": "lb"}}, {{"name": "olive oil", "product_id": 45, "quantity": "2", "unit": "tbsp"}}], "instructions": ["Heat {{1}} in a pan.", "Brown {{0}} for 6 minutes a side."], "estimated_prep_time": 30, "servings": {servings}}}"""

FEEDBACK = """
**A previous attempt at this dish was rejected. Fix this and write a NEW recipe:**
{feedback}
"""


def _deal_view(deal: Mapping[str, Any]) -> Dict[str, Any]:
    out = {"deal_id": deal["deal_id"], "product_name": deal.get("product_name", ""),
           "sale_price": deal.get("sale_price")}
    if deal.get("item"):
        # The stored label's generic item ("brussels sprouts"): a cleaner
        # display name than the flyer text. Display only; never decides.
        out["item"] = deal["item"]
    if deal.get("unit"):
        out["unit"] = deal["unit"]
    return out


def dish_inputs(dish: PlannedDish, deals: Mapping[int, Mapping[str, Any]],
                stock_up_ids: Sequence[int]) -> Tuple[List[Dict], List[Dict]]:
    """(this dish's deal rows, the stock-up rows not already in the dish)."""
    own = [dict(deals[i]) for i in dish.deal_ids() if i in deals]
    own_ids = {d["deal_id"] for d in own}
    stock = [dict(deals[i]) for i in stock_up_ids if i in deals and i not in own_ids]
    return own, stock


def build_prompt(dish: PlannedDish, own: List[Dict], stock: List[Dict],
                 pantry: Sequence[Dict[str, Any]], *, servings: int,
                 dietary_restrictions: Sequence[str],
                 preferences: Optional[Dict[str, Any]] = None,
                 feedback: Optional[str] = None) -> str:
    # The pantry the model sees is the dish's planned staples first, then the
    # rest of the confirmed pantry (it may need salt the planner forgot).
    planned = set(dish.pantry_product_ids)
    ordered = ([p for p in pantry if int(p["product_id"]) in planned]
               + [p for p in pantry if int(p["product_id"]) not in planned])
    return PROMPT.format(
        dish_name=dish.name,
        why=f"({dish.why})" if dish.why else "",
        dish_deals_json=json.dumps([_deal_view(d) for d in own]),
        stock_up_json=json.dumps([_deal_view(d) for d in stock]) if stock else "(none)",
        pantry_json=json.dumps([{"product_id": int(p["product_id"]), "name": p["name"]}
                                for p in ordered]),
        servings=servings,
        dietary_restrictions=", ".join(dietary_restrictions) or "none",
        combination_hints=hint_lines(combination_rule_hints(list(dietary_restrictions))),
        preferences=preferences_text(preferences),
        feedback=FEEDBACK.format(feedback=feedback) if feedback else "",
    )


def write_recipe(dish: PlannedDish, *, deals: Mapping[int, Mapping[str, Any]],
                 stock_up_ids: Sequence[int], pantry: Sequence[Dict[str, Any]],
                 servings: int, dietary_restrictions: Sequence[str],
                 preferences: Optional[Dict[str, Any]] = None,
                 deal_products: Optional[Mapping[str, Sequence[Dict]]] = None,
                 feedback: Optional[str] = None, spec: str,
                 chat_fn: llm_chat.ChatFn = llm_chat.chat,
                 recipe_id: RecipeIdFactory = random_recipe_id,
                 agent: str = "sous_chef",
                 ) -> Tuple[Recipe, List[llm_chat.Reply], str]:
    """``(recipe, every reply, prompt)`` for one dish.

    Raises :class:`llm_chat.ChatOutputError` when no reply validates and
    ``httpx`` errors when the model is unreachable.
    """
    own, stock = dish_inputs(dish, deals, stock_up_ids)
    prompt = build_prompt(dish, own, stock, pantry, servings=servings,
                          dietary_restrictions=dietary_restrictions,
                          preferences=preferences, feedback=feedback)
    context = citation_context([d["deal_id"] for d in own + stock],
                               [p["product_id"] for p in pantry])

    def validate(data: Dict[str, Any]) -> GeneratedRecipe:
        return RecipeBatch.model_validate(data, context=context).recipes[0]

    model, replies = llm_chat.chat_validated(
        chat_fn, spec, [{"role": "user", "content": prompt}], validate,
        temperature=TEMPERATURE, max_tokens=MAX_TOKENS, json_mode=True)
    staples = [{"product_id": int(p["product_id"]), "name": p["name"]} for p in pantry]
    # Steps render each cited deal by its linked product's canonical name, else
    # by its label item, else the flyer text (display only).
    shown = [{**d, "product_name": d.get("item") or d.get("product_name", "")}
             for d in own + stock]
    recipe = _to_recipe(agent, model, deals_by_id(shown), recipe_id, staples,
                        deal_products)
    name_from_citations(recipe, {d["deal_id"]: d for d in own + stock}, staples)
    return recipe, replies, prompt


def name_from_citations(recipe: Dict[str, Any], deals: Mapping[int, Mapping[str, Any]],
                        staples: Sequence[Mapping[str, Any]]) -> None:
    """Show each ingredient under the name of what it *cites*, not what the
    model typed: a model that writes "ground cinnamon" while citing the
    black-pepper staple must not list cinnamon. The model's wording is kept
    as ``written_as`` for debugging. Display only -- compliance and costing
    read the citation either way."""
    staple_names = {int(s["product_id"]): s["name"] for s in staples}
    for ing in recipe.get("ingredients", []):
        cited = None
        if ing.get("product_id") is not None:
            cited = staple_names.get(int(ing["product_id"]))
        elif ing.get("deal_id") is not None:
            deal = deals.get(int(ing["deal_id"])) or {}
            cited = deal.get("item") or deal.get("product_name")
        if cited and cited != ing.get("name"):
            ing["written_as"] = ing.get("name")
            ing["name"] = cited
