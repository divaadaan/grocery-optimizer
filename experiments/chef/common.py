"""What every arm shares: the scenario, the compact deal row, the plan contract, scoring.

The plan contract is dish-first (the owner's method, 2026-11-23): each dish
names its anchor deal(s) and its side deal(s); pantry items come only from a
fixed list the user is assumed to have confirmed; stock-up purchases are listed
separately. No budget: the product is budget-agnostic (owner, 2026-11-23);
cost is reported for information only.

Scoring is deterministic -- no LLM judges anything. Dish *quality* is left to
the owner's review sheet (``review.md`` per run).
"""
from __future__ import annotations

import collections
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PANTRY = [
    "salt", "black pepper", "vegetable oil", "olive oil", "butter", "all-purpose flour",
    "sugar", "garlic", "yellow onion", "dried oregano", "dried basil", "paprika",
    "ground cumin", "chili flakes", "soy sauce", "vinegar", "dijon mustard",
    "chicken or vegetable stock", "rice", "eggs",
]


@dataclass
class Scenario:
    snapshot: str
    budget: float = 100.0
    household_size: int = 2
    num_meals: int = 5
    meal_type: str = "dinner"
    pantry: list[str] = field(default_factory=lambda: list(DEFAULT_PANTRY))

    def deals(self) -> list[dict]:
        return json.loads(Path(self.snapshot).read_text())["deals"]

    def describe(self) -> str:
        # Budget-agnostic framing (owner, 2026-11-23): "here's what you can eat
        # from grocery sales near you". Cost is reported, never constrained.
        return (f"- Household: {self.household_size} people\n"
                f"- Plan: {self.num_meals} {self.meal_type}s\n"
                f"- Pantry the user already has (use freely, cost $0): {', '.join(self.pantry)}")


def deal_row(d: dict, name_len: int = 90) -> str:
    """One line per deal: ``id | name | $price/unit | category | chain``."""
    name = " ".join(d["product_name"].split())
    if len(name) > name_len:
        name = name[: name_len - 1] + "…"
    price = f"${d['sale_price']:.2f}" if d.get("sale_price") is not None else "$?"
    if d.get("unit"):
        price += f"/{d['unit']}"
    return f"{d['deal_id']} | {name} | {price} | {d.get('category') or '-'} | {d.get('chain') or d.get('store_name')}"


PLAN_CONTRACT = """Return ONLY a JSON object of this shape (no comments, no code fences):
{
  "dishes": [
    {
      "name": "Roast chicken with garlic potatoes",
      "anchor_deal_ids": [123],
      "side_deal_ids": [456, 789],
      "pantry_items": ["salt", "olive oil"],
      "why": "one sentence"
    }
  ],
  "stock_up_deal_ids": [321],
  "notes": "one or two sentences on the overall plan"
}
Field meanings: anchor_deal_ids = the main component(s) the dish is built around; side_deal_ids =
sides and supporting ingredients bought this week; pantry_items = ONLY items from the user's pantry
list; stock_up_deal_ids = discounted staples worth buying extra of for future weeks.
Rules: exactly {num_meals} dishes. Every deal id must be copied from the deals you were shown -- never
invent one. A deal bought once may be used in several dishes. Build every dish from real cooking
ingredients on sale."""


@dataclass
class Score:
    parse_ok: bool
    num_dishes: int = 0
    dishes_ok: bool = False
    cited_ids: int = 0
    invalid_ids: list = field(default_factory=list)
    bad_pantry: list = field(default_factory=list)
    dishes_without_anchor: int = 0
    total_cost: float = 0.0
    within_budget: bool = False
    chains: dict = field(default_factory=dict)
    categories: dict = field(default_factory=dict)
    beverage_ids: list = field(default_factory=list)
    reused_deals: int = 0
    anchor_categories: dict = field(default_factory=dict)
    # Where the cited deals sat in the list the model was shown (0 = top, 1 = bottom).
    # A flat spread is healthy; a cluster at either end is position bias.
    position_quintiles: list = field(default_factory=list)
    # Valid ids the model cited but was never shown (tool arms): recalled or guessed.
    cited_unseen: list = field(default_factory=list)


def score_plan(plan: dict | None, scenario: Scenario, presented_ids: list | None = None) -> Score:
    if not isinstance(plan, dict) or not isinstance(plan.get("dishes"), list):
        return Score(parse_ok=False)
    by_id = {d["deal_id"]: d for d in scenario.deals()}
    pantry = {p.lower() for p in scenario.pantry}
    s = Score(parse_ok=True, num_dishes=len(plan["dishes"]))
    s.dishes_ok = s.num_dishes == scenario.num_meals
    cited: list = []
    use_count = collections.Counter()
    for dish in plan["dishes"]:
        anchors = _ids(dish.get("anchor_deal_ids"))
        sides = _ids(dish.get("side_deal_ids"))
        if not anchors:
            s.dishes_without_anchor += 1
        for i in set(anchors + sides):
            use_count[i] += 1
        cited += anchors + sides
        for p in dish.get("pantry_items") or []:
            if str(p).lower() not in pantry:
                s.bad_pantry.append(p)
    cited += _ids(plan.get("stock_up_deal_ids"))
    distinct = list(dict.fromkeys(cited))
    s.cited_ids = len(distinct)
    s.invalid_ids = [i for i in distinct if i not in by_id]
    valid = [by_id[i] for i in distinct if i in by_id]
    s.total_cost = round(sum(d["sale_price"] or 0 for d in valid), 2)
    s.within_budget = s.total_cost <= scenario.budget
    s.chains = dict(collections.Counter(d["chain"] for d in valid).most_common())
    s.categories = dict(collections.Counter(d["category"] for d in valid).most_common())
    s.beverage_ids = [d["deal_id"] for d in valid if d["category"] == "Beverages"]
    s.reused_deals = sum(1 for c in use_count.values() if c > 1)
    anchors = {i for dish in plan["dishes"] for i in _ids(dish.get("anchor_deal_ids"))}
    s.anchor_categories = dict(collections.Counter(
        by_id[i]["category"] for i in anchors if i in by_id).most_common())
    if presented_ids:
        pos = {i: n / max(len(presented_ids) - 1, 1) for n, i in enumerate(presented_ids)}
        q = [0] * 5
        for i in distinct:
            if i in pos:
                q[min(int(pos[i] * 5), 4)] += 1
        s.position_quintiles = q
        s.cited_unseen = [i for i in distinct if i in by_id and i not in pos]
    return s


def _ids(v) -> list:
    out = []
    for x in v or []:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            out.append(x)  # kept so it shows up as invalid
    return out


def review_markdown(plan: dict | None, scenario: Scenario, header: str) -> str:
    """Human-readable plan for the owner's dish-quality review."""
    if not isinstance(plan, dict):
        return f"## {header}\n\n_(no parseable plan)_\n"
    by_id = {d["deal_id"]: d for d in scenario.deals()}

    def show(i):
        d = by_id.get(_ids([i])[0])
        return f"`{i}` {deal_row(d, 60).split(' | ', 1)[1]}" if d else f"`{i}` **INVALID ID**"

    lines = [f"## {header}", ""]
    for n, dish in enumerate(plan.get("dishes") or [], 1):
        lines.append(f"**{n}. {dish.get('name')}** — {dish.get('why', '')}")
        for i in dish.get("anchor_deal_ids") or []:
            lines.append(f"- anchor: {show(i)}")
        for i in dish.get("side_deal_ids") or []:
            lines.append(f"- side: {show(i)}")
        if dish.get("pantry_items"):
            lines.append(f"- pantry: {', '.join(map(str, dish['pantry_items']))}")
        lines.append("")
    if plan.get("stock_up_deal_ids"):
        lines.append("**Stock up:**")
        lines += [f"- {show(i)}" for i in plan["stock_up_deal_ids"]]
        lines.append("")
    if plan.get("notes"):
        lines += [f"_Notes:_ {plan['notes']}", ""]
    return "\n".join(lines)


def as_dict(score: Score) -> dict:
    return asdict(score)
