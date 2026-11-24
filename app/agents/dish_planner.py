"""Plan the week's dishes from the user's confirmed trays (one LLM call).

Approach C (``experiments/chef/DESIGN_APPROACH_C.md`` §6); replaces
the old ``ChefOrchestrator.plan_ingredient_groups``. The model sees ~55 short tray rows
the user already confirmed, and returns the dish-first plan the prototype used
(``experiments/chef/common.py``): each dish names its anchor and side deal ids
and the pantry product ids it uses, plus stock-up deal ids.

Everything the model returns is checked in code (:class:`DishPlan` with a
validation context): exactly ``num_meals`` dishes, at least one anchor each,
anchors from the ANCHORS tray, every other id one the user kept or confirmed.
Stray ids are dropped and recorded (:func:`repair_plan`) rather than failing
the plan; a dish with no valid anchor is dropped, so a plan can come back with
fewer than ``num_meals`` dishes (the 60% partial rule then applies). A plan
with no usable dish is retried once.
An anchor may serve more than one dish (owner, 2026-11-23). One retry with the
error, then the run fails. Nothing here judges dietary compliance: the trays
are already gated, and the recipe verdict is ``app/agents/dietary.py``.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from app.agents import llm_chat
from app.agents.prompts import preferences_text
from app.services.trays import TRAYS, TrayItem, tray_row

PLAN_CONTEXT_KEY = "dish_plan"
MAX_TOKENS = 3000
TEMPERATURE = 0.4

SYSTEM = """You are a home cook planning meals from this week's grocery flyers near the user.
The user has already chosen the deals they want, sorted into trays:
- ANCHORS: the best protein deals this week, each the cheapest for its item across nearby stores.
- SIDES: vegetables, starches, fruit and dairy on sale.
- STOCK-UP: discounted pantry staples worth buying extra of.
Build each dish around an anchor, pair it with sensible sides, and pick stock-up items worth buying.
Vary the anchors across the week where you can; an anchor may serve more than one dish if there are
fewer anchors than dishes. Vary the sides too: give each dish sides that suit it, and do not repeat
the same sides in every dish. Only real meals from real cooking ingredients."""

CONTRACT = """Return ONLY a JSON object of this shape (no comments, no code fences):
{{"dishes": [{{"name": "Roast chicken with garlic potatoes", "anchor_deal_ids": [123], "side_deal_ids": [456, 789], "pantry_product_ids": [12, 15], "why": "one sentence"}}], "stock_up_deal_ids": [321], "notes": "one or two sentences on the overall plan"}}
Field meanings: anchor_deal_ids = the main component(s) the dish is built around, from ANCHORS;
side_deal_ids = sides and supporting ingredients from SIDES or STOCK-UP; pantry_product_ids = ONLY
product ids from the user's pantry list; stock_up_deal_ids = STOCK-UP deals worth buying extra of.
Rules: exactly {num_meals} dishes. Every id must be copied from the lists you were shown -- never
invent one."""


class PlannedDish(BaseModel):
    name: str
    anchor_deal_ids: List[int] = Field(min_length=1)
    side_deal_ids: List[int] = Field(default_factory=list)
    pantry_product_ids: List[int] = Field(default_factory=list)
    why: str = ""

    @field_validator("name", "why", mode="before")
    @classmethod
    def _text(cls, v):
        return "" if v is None else str(v).strip()

    @field_validator("anchor_deal_ids", "side_deal_ids", "pantry_product_ids", mode="before")
    @classmethod
    def _ids(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            v = [v]
        out = []
        for x in v:
            if isinstance(x, bool):
                raise ValueError(f"id {x!r} is not a number")
            out.append(int(str(x).strip()))
        return list(dict.fromkeys(out))  # de-duplicate, keep order

    def deal_ids(self) -> List[int]:
        return list(dict.fromkeys(self.anchor_deal_ids + self.side_deal_ids))


class DishPlan(BaseModel):
    dishes: List[PlannedDish] = Field(min_length=1)   # repair_plan may leave fewer than N
    stock_up_deal_ids: List[int] = Field(default_factory=list)
    notes: str = ""
    #: Optional-field ids removed by :func:`repair_plan` (recorded, never used).
    dropped: List[Dict[str, Any]] = Field(default_factory=list)

    @field_validator("stock_up_deal_ids", mode="before")
    @classmethod
    def _ids(cls, v):
        return PlannedDish._ids(v)

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, v):
        return "" if v is None else str(v)

    @model_validator(mode="after")
    def _offered(self, info: ValidationInfo):
        ctx = (info.context or {}).get(PLAN_CONTEXT_KEY)
        if ctx is None:
            return self
        problems = []
        if len(self.dishes) > ctx["num_meals"]:
            problems.append(f"expected at most {ctx['num_meals']} dishes, got {len(self.dishes)}")
        for i, dish in enumerate(self.dishes):
            label = f"dish {i + 1} ({dish.name!r})"
            if not dish.name:
                problems.append(f"dish {i + 1} has no name")
            bad = [d for d in dish.anchor_deal_ids if d not in ctx["anchors"]]
            if bad:
                problems.append(f"{label} anchor_deal_ids {bad} are not ANCHORS")
            bad = [d for d in dish.side_deal_ids if d not in ctx["deals"]]
            if bad:
                problems.append(f"{label} side_deal_ids {bad} were not offered")
            bad = [p for p in dish.pantry_product_ids if p not in ctx["pantry"]]
            if bad:
                problems.append(f"{label} pantry_product_ids {bad} are not in the pantry list")
        bad = [d for d in self.stock_up_deal_ids if d not in ctx["stock_up"]]
        if bad:
            problems.append(f"stock_up_deal_ids {bad} are not STOCK-UP deals")
        if problems:
            raise ValueError("; ".join(problems) + ". Use only ids from the lists shown.")
        return self


def plan_context(kept: Dict[str, List[TrayItem]], pantry: Sequence[Dict[str, Any]],
                 num_meals: int) -> Dict[str, Any]:
    return {PLAN_CONTEXT_KEY: {
        "num_meals": num_meals,
        "anchors": frozenset(t.deal_id for t in kept["anchors"]),
        "stock_up": frozenset(t.deal_id for t in kept["stock_up"]),
        "deals": frozenset(t.deal_id for name in TRAYS for t in kept[name]),
        "pantry": frozenset(int(p["product_id"]) for p in pantry),
    }}


def build_prompt(kept: Dict[str, List[TrayItem]], pantry: Sequence[Dict[str, Any]],
                 num_meals: int, household_size: int,
                 preferences: Optional[Dict[str, Any]] = None) -> str:
    sections = []
    for name, title in (("anchors", "ANCHORS"), ("sides", "SIDES"), ("stock_up", "STOCK-UP")):
        rows = "\n".join(tray_row(t) for t in kept[name]) or "(none)"
        sections.append(f"{title} (id | name | price | store | item; comparison):\n{rows}")
    pantry_rows = "\n".join(f"{p['product_id']} | {p['name']}" for p in pantry) or "(none)"
    n_anchors = len(kept["anchors"])
    reuse = ""
    if 0 < n_anchors < num_meals:
        # A 7B planner otherwise invents dishes around a side to reach the
        # count (2026-11-23 live check); those dishes are dropped.
        ids = ", ".join(str(t.deal_id) for t in kept["anchors"])
        reuse = (f"\nThere are only {n_anchors} ANCHORS for {num_meals} dishes, so REUSE them: "
                 f"every dish's anchor_deal_ids must be one of [{ids}], and some anchors "
                 "will appear in more than one dish (cooked a different way).\n")
    return (
        f"{SYSTEM}\n\n"
        f"- Household: {household_size} people\n"
        f"- Plan: {num_meals} dinners\n"
        f"- Preferences: {preferences_text(preferences)}\n\n"
        + "\n\n".join(sections)
        + f"\n\nPANTRY the user already has (product_id | name; cost $0):\n{pantry_rows}\n"
        + reuse + "\n"
        + CONTRACT.format(num_meals=num_meals)
    )


def repair_plan(data: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Drop ids the plan may not use from the *optional* fields, recording
    each in ``dropped`` -- before validation, so a stray side or pantry id
    costs one ingredient instead of the whole plan.

    Side, pantry and stock-up ids outside what was offered are removed (the
    2026-11-23 live check: a 7B planner cited a pantry id as a side, or a
    side as an anchor, in about one run in four). Anchors are never repaired:
    a kept side cited as an anchor moves to the dish's sides, and a dish left
    with no ANCHORS id is dropped (recorded) rather than given a protein the
    model never chose; dishes beyond ``num_meals`` are dropped too. A plan
    with no usable dish fails validation and goes back to the model. Ids only
    -- nothing reads a name.
    """
    ctx = context[PLAN_CONTEXT_KEY]
    dropped: List[Dict[str, Any]] = []

    def keep(values, allowed, where):
        out = []
        for v in values if isinstance(values, list) else []:
            try:
                i = int(str(v).strip())
            except (TypeError, ValueError):
                out.append(v)            # let schema validation report it
                continue
            if i in allowed:
                out.append(i)
            else:
                dropped.append({"where": where, "id": i})
        return out

    data = dict(data)
    dishes = []
    for n, dish in enumerate(data.get("dishes") or []):
        if not isinstance(dish, dict):
            dishes.append(dish)
            continue
        dish = dict(dish)
        anchors = dish.get("anchor_deal_ids")
        if isinstance(anchors, list):
            ids = [int(a) for a in anchors if str(a).strip().lstrip("-").isdigit()]
            good = [a for a in ids if a in ctx["anchors"]]
            moved = [a for a in ids if a not in ctx["anchors"] and a in ctx["deals"]]
            if not good:
                # No ANCHORS id at all: the dish is not built around this
                # week's protein deals. Drop it rather than assign a protein
                # the model never chose; the run continues with the rest.
                dropped.append({"where": f"dish {n + 1}", "id": None,
                                "reason": "no anchor from ANCHORS",
                                "name": str(dish.get("name") or "")[:80]})
                continue
            dish["anchor_deal_ids"] = good
            if moved:  # a kept side cited as an anchor: keep it as a side
                sides = dish.get("side_deal_ids") if isinstance(dish.get("side_deal_ids"), list) else []
                dish["side_deal_ids"] = list(dict.fromkeys(list(sides) + moved))
        if "side_deal_ids" in dish:
            dish["side_deal_ids"] = keep(dish["side_deal_ids"], ctx["deals"],
                                         f"dish {n + 1} side_deal_ids")
        if "pantry_product_ids" in dish:
            dish["pantry_product_ids"] = keep(dish["pantry_product_ids"], ctx["pantry"],
                                              f"dish {n + 1} pantry_product_ids")
        dishes.append(dish)
    if "dishes" in data:
        extra = dishes[ctx["num_meals"]:]
        for n, dish in enumerate(extra, start=ctx["num_meals"] + 1):
            dropped.append({"where": f"dish {n}", "id": None, "reason": "more dishes than meals",
                            "name": str((dish or {}).get("name") or "")[:80]
                            if isinstance(dish, dict) else ""})
        data["dishes"] = dishes[:ctx["num_meals"]]
    if "stock_up_deal_ids" in data:
        data["stock_up_deal_ids"] = keep(data["stock_up_deal_ids"], ctx["stock_up"],
                                         "stock_up_deal_ids")
    data["dropped"] = dropped
    return data


def plan_dishes(kept: Dict[str, List[TrayItem]], pantry: Sequence[Dict[str, Any]], *,
                num_meals: int, household_size: int,
                preferences: Optional[Dict[str, Any]] = None, spec: str,
                chat_fn: llm_chat.ChatFn = llm_chat.chat
                ) -> Tuple[DishPlan, List[llm_chat.Reply], str]:
    """``(validated plan, every reply, prompt)``.

    Raises :class:`llm_chat.ChatOutputError` when no reply validates (model
    output) and ``httpx`` errors when the model is unreachable (infrastructure).
    """
    prompt = build_prompt(kept, pantry, num_meals, household_size, preferences)
    context = plan_context(kept, pantry, num_meals)
    plan, replies = llm_chat.chat_validated(
        chat_fn, spec, [{"role": "user", "content": prompt}],
        lambda data: DishPlan.model_validate(repair_plan(data, context), context=context),
        temperature=TEMPERATURE, max_tokens=MAX_TOKENS, json_mode=True)
    return plan, replies, prompt


def plan_as_dict(plan: DishPlan) -> Dict[str, Any]:
    return json.loads(plan.model_dump_json())
