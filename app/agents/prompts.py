"""Prompt text helpers shared by the dish planner and the per-dish SousChef."""
import json
from typing import Iterable, List

from ..services import products


def _attribute_noun(attribute: str) -> str:
    """``contains_meat`` -> ``meat``, ``contains_animal_byproduct`` ->
    ``animal byproduct``: the attribute's own name, for prompt text."""
    return attribute.removeprefix("contains_").replace("_", " ")


def _join(words: List[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def combination_rule_hints(dietary_restrictions: Iterable[str] | None,
                           unit: str = "recipe") -> List[str]:
    """Prompt hints for the user's recipe-level combination rules.

    Derived from :data:`app.services.products.RESTRICTION_COMBINATION_RULES`
    (today: kosher's meat + dairy), so a future rule gets a hint with no prompt
    edit. The hint only cuts retries: whether a recipe complies is still
    decided in code from the cited products' curated attributes
    (``app/agents/dietary.py``), never by the model. ``unit`` names what may
    not mix -- "recipe" for a SousChef, "group" for the Chef.
    """
    hints: List[str] = []
    seen = set()
    for label in dietary_restrictions or []:
        key = (label or "").strip().lower()
        for rule in products.RESTRICTION_COMBINATION_RULES.get(key, ()):
            if (key, rule) in seen:
                continue
            seen.add((key, rule))
            nouns = _join([_attribute_noun(a) for a in rule])
            hints.append(f"{key}: never combine {nouns} in the same {unit} "
                         "(checked in code from product attributes)")
    return hints


def hint_lines(hints: List[str]) -> str:
    """Hints as extra ``- `` bullet lines appended to a requirements bullet;
    empty when there are none, so prompts without a rule are unchanged."""
    return "".join(f"\n- {h}" for h in hints)


def preferences_text(preferences) -> str:
    """User preferences (cuisines, meal types, ingredients to avoid) as prompt
    text. Soft guidance for the model only: they never decide compliance."""
    return json.dumps(preferences, sort_keys=True) if preferences else "none"
