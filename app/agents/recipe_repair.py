"""Deterministic repair of a SousChef reply, and concrete retry feedback.

The SousChef (``app/agents/dish_writer.py``) loses a dish when its reply fails
``RecipeBatch`` validation twice. Two failure shapes are repaired here in
code, and only these two, because each is provably safe:

1. **An ingredient with no valid citation**: both ``deal_id`` and
   ``product_id`` are absent, null, or a non-numeric string (``"N/A"``,
   ``""``, ``"none"``). The ingredient is dropped, every placeholder that
   references it is removed from the steps, and the remaining placeholders
   are renumbered to the shortened list.
2. **A placeholder past the end of the ingredient list** (``{12}`` with 11
   ingredients). It refers to nothing; it is removed from the step.

Safety: a repair only ever *removes*. It never adds a citation, never
changes the id an ingredient cites, and never keeps an uncited ingredient,
so the repaired recipe's citations are a subset of the reply's own valid
citations and the dietary verdict (``app/agents/dietary.py``) still runs on
them unchanged. An ingredient that cites one valid id beside junk in the
other field, or an id that was not offered, is *not* repaired: the model's
intent is unclear, so it goes back to the model.

Removing a placeholder: when it sits in a list with another placeholder
(``{3}, {4}, and {5}``) it is removed with its list separator; otherwise the
step was about that ingredient (``Add {2} and stir-fry``) and the whole step
is dropped. Only separators (``,`` ``and`` ``or`` ``&``) are matched; no
ingredient or food word is ever matched, so nothing here decides dietary
access (CLAUDE.md *Product framing*).

A repair is refused, and the reply goes back to the model, when the result
would no longer be the dish that was asked for:

* fewer than :data:`MIN_INGREDIENTS` ingredients remain (the anchor plus two
  others: below that it is not a recipe, and the prompt's own example has
  two cited items besides any seasoning);
* more than half of the ingredients, or more than half of the steps, would
  be dropped (most of the reply was not from the lists: ask again);
* no remaining ingredient cites one of the dish's anchor deals;
* the placeholders look 1-based (``{N}`` used, ``{0}`` never), where every
  "in range" index is off by one and dropping only the last would hide it.

Pure functions over the parsed JSON, standard library plus the shared
placeholder parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .instruction_refs import parse_step

#: The smallest recipe a repair may leave: the anchor plus two other items.
MIN_INGREDIENTS = 3

_SPLIT = re.compile(r"(\{\d+\})")
#: Text between two placeholders that only separates them in a list.
_SEPARATOR = re.compile(r"^\s*(?:,\s*)?(?:(?:and|or|&)\s+)?$", re.IGNORECASE)


# ---------------------------------------------------------------- citations

def _is_missing(value: Any) -> bool:
    """A citation field that holds no id at all: absent/None, or a string
    that is not a non-negative integer ("N/A", "", "none", "-1"). Numbers,
    bools and containers are *not* missing: they go to the schema, which
    accepts or rejects them with its own message."""
    if value is None:
        return True
    return isinstance(value, str) and not value.strip().isdigit()


def uncited(ingredient: Any) -> bool:
    """True when an ingredient object cites nothing usable in either field."""
    return (isinstance(ingredient, dict)
            and _is_missing(ingredient.get("deal_id"))
            and _is_missing(ingredient.get("product_id")))


def _recipe_of(data: Any) -> Tuple[Optional[Dict[str, Any]], Any]:
    """``(the first recipe object, a function rebuilding data around a
    replacement)`` for the shapes ``RecipeBatch`` accepts: a single recipe,
    ``{"recipes": [...]}``, a bare list. ``(None, None)`` for anything else
    (left to the schema)."""
    def looks(obj):
        return isinstance(obj, dict) and "name" in obj and (
            "ingredients" in obj or "instructions" in obj)

    if isinstance(data, dict) and isinstance(data.get("recipes"), list) and data["recipes"] \
            and isinstance(data["recipes"][0], dict):
        return data["recipes"][0], lambda r: {**data, "recipes": [r, *data["recipes"][1:]]}
    if looks(data):
        return data, lambda r: r
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0], lambda r: [r, *data[1:]]
    return None, None


# ---------------------------------------------------------------- repair

@dataclass
class Repair:
    """The outcome of :func:`repair_reply`. ``data`` is the repaired reply
    (``None`` when nothing was repairable or the repair was refused);
    ``repairs`` lists what was changed; ``refused`` says why not."""
    data: Any = None
    repairs: List[Dict[str, Any]] = field(default_factory=list)
    refused: Optional[str] = None


def _remove(parts: List[str], k: int) -> bool:
    """Remove the placeholder at ``parts[k]`` (odd index) with its list
    separator. False when it is not in a list with another placeholder."""
    if k >= 2 and _SEPARATOR.match(parts[k - 1]):
        del parts[k - 1:k + 1]
        return True
    if k + 2 < len(parts) and _SEPARATOR.match(parts[k + 1]):
        del parts[k:k + 2]
        return True
    return False


def _repair_step(template: str, gone: Mapping[int, str], renumber: Mapping[int, int]
                 ) -> Tuple[Optional[str], List[Tuple[int, str]]]:
    """``(new template or None if the step is dropped, [(index, reason)])``."""
    parts = _SPLIT.split(template)
    removed: List[Tuple[int, str]] = []
    k = 1
    while k < len(parts):
        index = int(parts[k][1:-1])
        if index in gone:
            removed.append((index, gone[index]))
            if not _remove(parts, k):
                return None, removed
            continue  # parts shifted; re-read position k
        parts[k] = "{%d}" % renumber[index]
        k += 2
    return "".join(parts), removed


def repair_reply(data: Any, anchor_deal_ids: Iterable[int] = ()) -> Repair:
    """Repair the first recipe in a parsed SousChef reply (module docstring).

    Returns ``Repair(data=None)`` with no repairs when there is nothing this
    module repairs, and ``Repair(data=None, refused=...)`` when a repair was
    possible but would leave something that is no longer the dish.
    ``anchor_deal_ids`` empty skips the anchor check (a scorer that cannot
    tell the anchors apart)."""
    recipe, rebuild = _recipe_of(data)
    if recipe is None:
        return Repair()
    ings, steps = recipe.get("ingredients"), recipe.get("instructions")
    if isinstance(steps, str):
        steps = [steps]
    if not isinstance(ings, list) or not isinstance(steps, list) \
            or not all(isinstance(s, str) for s in steps):
        return Repair()

    n = len(ings)
    refs = [r for s in steps for r in parse_step(s).refs]
    gone: Dict[int, str] = {i: "uncited ingredient" for i, ing in enumerate(ings)
                            if uncited(ing)}
    gone.update({r: "out of range" for r in refs if r >= n})
    if not gone:
        return Repair()
    if refs and 0 not in refs and n in refs:
        return Repair(refused="placeholders look 1-based ({%d} used, {0} never)" % n)

    kept = [i for i in range(n) if i not in gone]
    renumber = {old: new for new, old in enumerate(kept)}
    repairs: List[Dict[str, Any]] = [
        {"kind": "dropped_ingredient", "index": i, "name": str(ings[i].get("name", "")),
         "deal_id": ings[i].get("deal_id"), "product_id": ings[i].get("product_id")}
        for i in sorted(gone) if i < n]

    new_steps: List[str] = []
    for number, template in enumerate(steps, 1):
        new, removed = _repair_step(template, gone, renumber)
        for index, reason in removed:
            repairs.append({"kind": "dropped_placeholder", "step": number,
                            "index": index, "reason": reason})
        if new is None:
            repairs.append({"kind": "dropped_step", "step": number, "template": template})
        else:
            new_steps.append(new)
    if any(old != new for old, new in renumber.items()):
        repairs.append({"kind": "renumbered_placeholders",
                        "map": {str(o): v for o, v in renumber.items() if o != v}})

    dropped_ings = n - len(kept)
    dropped_steps = len(steps) - len(new_steps)
    anchors = {int(a) for a in anchor_deal_ids}
    if len(kept) < MIN_INGREDIENTS:
        refused = f"only {len(kept)} ingredient(s) would remain (minimum {MIN_INGREDIENTS})"
    elif dropped_ings * 2 > n:
        refused = f"{dropped_ings} of {n} ingredients would be dropped"
    elif dropped_steps * 2 > len(steps):
        refused = f"{dropped_steps} of {len(steps)} steps would be dropped"
    elif anchors and not any(_cites_deal(ings[i], anchors) for i in kept):
        refused = "no remaining ingredient cites the dish's anchor deal"
    else:
        refused = None
    if refused:
        return Repair(repairs=repairs, refused=refused)

    fixed = {**recipe, "ingredients": [ings[i] for i in kept], "instructions": new_steps}
    return Repair(data=rebuild(fixed), repairs=repairs)


def _cites_deal(ingredient: Any, deal_ids: set) -> bool:
    value = ingredient.get("deal_id") if isinstance(ingredient, dict) else None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    return isinstance(value, int) and not isinstance(value, bool) and value in deal_ids


# ---------------------------------------------------------------- feedback

def reply_problems(data: Any, allowed_deals: Optional[Sequence[int]] = None,
                   allowed_products: Optional[Sequence[int]] = None) -> List[str]:
    """Concrete, actionable problems in a SousChef reply, all at once.

    Pydantic reports ingredient errors first and never reaches the step
    check while one is open, so the model was told about a ``{12}`` only
    after it had used its retry (2026-11-24 live baseline). This reads the
    raw reply directly. Empty when nothing here applies (the caller then
    falls back to the schema's own message)."""
    recipe, _ = _recipe_of(data)
    if recipe is None:
        return []
    ings = recipe.get("ingredients")
    steps = recipe.get("instructions")
    if isinstance(steps, str):
        steps = [steps]
    if not isinstance(ings, list) or not isinstance(steps, list):
        return []
    deals = set(allowed_deals) if allowed_deals is not None else None
    products = set(allowed_products) if allowed_products is not None else None
    out: List[str] = []
    for i, ing in enumerate(ings):
        if not isinstance(ing, dict):
            out.append(f"ingredient {i} is not an object")
            continue
        name = ing.get("name")
        deal, product = ing.get("deal_id"), ing.get("product_id")
        if uncited(ing):
            shown = deal if deal is not None else product
            what = f" (it has {shown!r})" if shown is not None else ""
            out.append(f"ingredient {i} {name!r} cites no listed id{what}: it is not on "
                       "your lists, so REMOVE it from \"ingredients\" and from the steps; "
                       "never cite another item's id for it")
            continue
        if not _is_missing(deal) and not _is_missing(product):
            out.append(f"ingredient {i} {name!r} has both a deal_id and a product_id: "
                       "keep only the one written on its own line above")
            continue
        junk = [(k, v) for k, v in (("deal_id", deal), ("product_id", product))
                if isinstance(v, str) and v.strip() and not v.strip().isdigit()]
        if junk:
            key, value = junk[0]
            out.append(f"ingredient {i} {name!r} has {key} {value!r}: delete that field and "
                       "cite only the id on the ingredient's own line, or remove the "
                       "ingredient")
            continue
        for key, value, allowed in (("deal_id", deal, deals), ("product_id", product, products)):
            if _is_missing(value):
                continue
            as_int = int(value.strip()) if isinstance(value, str) else value
            if not isinstance(as_int, int) or isinstance(as_int, bool):
                out.append(f"ingredient {i} {name!r} has {key} {value!r}: write the "
                           "number from its line above, or remove the ingredient")
            elif allowed is not None and as_int not in allowed:
                out.append(f"ingredient {i} {name!r} cites {key} {as_int}, which is not "
                           "listed above: use the id on its own line, or remove it")
    n = len(ings)
    valid = "{0}..{%d}" % (n - 1) if n else "none"
    any_ref = False
    for number, step in enumerate(steps, 1):
        if not isinstance(step, str):
            out.append(f"step {number} is not a string")
            continue
        parsed = parse_step(step)
        any_ref = any_ref or bool(parsed.refs)
        for r in sorted({r for r in parsed.refs if r >= n}):
            out.append(f"step {number} uses {{{r}}}, but there are only {n} ingredients, "
                       f"so ingredient {r} does not exist; valid placeholders are {valid}. "
                       "Add the ingredient to the list (if it is listed above) or remove "
                       "the placeholder")
        if parsed.malformed:
            out.append(f"step {number} has malformed placeholder(s) {parsed.malformed}: "
                       "write an ingredient only as {N}, and use no other braces")
    if steps and not any_ref:
        out.append(f"no step uses a placeholder: write every ingredient as {valid}")
    return out


def clean_schema_error(text: str) -> str:
    """A pydantic message without its documentation links and input dumps."""
    lines = []
    for line in str(text).splitlines():
        if "errors.pydantic.dev" in line:
            continue
        lines.append(re.sub(r"\s*\[type=.*$", "", line))
    return "\n".join(lines).strip()


__all__ = ["MIN_INGREDIENTS", "Repair", "clean_schema_error",
           "repair_reply", "reply_problems", "uncited"]
