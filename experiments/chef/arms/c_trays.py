"""Arm C: code builds the trays (anchors / sides / stock-up), the Chef only plans dishes from them.

The owner's method made deterministic where it can be:
1. Anchors = protein deals that are cheapest for their item across stores
   (cross-store comparison on a normalised price basis).
2. Stock-up = discounted pantry staples, same comparison.
3. Sides = cheapest vegetable / starch / fruit per item.

Input labels (item identity + culinary role) come from ``experiments/chef/label.py``
-- in production a reviewed product attribute. Prepared meals, snacks,
beverages and non-food never reach a tray. The Chef sees ~55 short rows.
"""
from __future__ import annotations

import json
import re
import statistics
from pathlib import Path

from experiments.chef.common import PLAN_CONTRACT, Scenario, deal_row
from experiments.chef.llm import chat, parse_json

NAME = "C_trays"
N_ANCHORS, N_SIDES, N_STAPLES = 15, 25, 15
LB_PER_KG = 2.20462

SYSTEM = """You are a home cook planning a week of meals from this week's grocery flyers near the user.
The deals have already been sorted into trays for you:
- ANCHORS: the best protein deals this week, each the cheapest for its item across nearby stores.
- SIDES: the cheapest vegetables, starches and fruit.
- STOCK-UP: discounted pantry staples worth buying extra of.
Build each dish around an anchor, pair it with sensible sides, and pick stock-up items worth buying.
Vary the anchors across the week. Only real meals from real cooking ingredients."""


def price_basis(d: dict) -> tuple[str, float] | None:
    """(basis, comparable price): per-lb for weight prices, else per-package."""
    p = d.get("sale_price")
    if p is None:
        return None
    u = (d.get("unit") or "").strip().lower()
    if u.startswith("lb"):
        return "lb", p
    if re.fullmatch(r"100\s?g", u):
        return "lb", p * 4.5359
    if re.fullmatch(r"[\d.]+\s?/?\s?kg!?", u):
        # Flyer convention: "$3.49 / 7.69kg" -- the sale price is per lb, the unit
        # text is the same price per kg.
        return "lb", p
    return "pkg", p


def _norm_item(item: str, all_items: set) -> str:
    return item[:-1] if item.endswith("s") and item[:-1] in all_items else item


def build_trays(deals: list[dict], labels: dict) -> dict[str, list[dict]]:
    items = {v["item"] for v in labels.values()}
    groups: dict[tuple, list[dict]] = {}
    for d in deals:
        lab = labels.get(str(d["deal_id"]))
        pb = price_basis(d)
        if not lab or not pb:
            continue
        key = (lab["role"], _norm_item(lab["item"], items), pb[0])
        groups.setdefault(key, []).append({**d, "_item": key[1], "_basis": pb[0], "_cmp": pb[1]})

    best = []  # one row per (role, item, basis): the cheapest deal + how it compares
    for (role, item, basis), rows in groups.items():
        rows.sort(key=lambda r: r["_cmp"])
        top = rows[0]
        per_store = {}
        for r in rows:
            per_store.setdefault(r["chain"] or r["store_name"], r["_cmp"])
        n = len(per_store)
        med = statistics.median(per_store.values())
        gap = 1 - top["_cmp"] / med if n >= 2 and med > 0 else 0.0
        best.append({**top, "_role": role, "_stores": n, "_median": med, "_gap": gap})

    def pick(roles, k, need_compare=False):
        rows = [b for b in best if b["_role"] in roles and (b["_stores"] >= 2 or not need_compare)]
        # Biggest cross-store gap first; then cheapest per lb before per-pkg.
        rows.sort(key=lambda b: (-round(b["_gap"], 2), b["_basis"] != "lb", b["_cmp"]))
        return rows[:k]

    return {"ANCHORS": pick({"protein"}, N_ANCHORS),
            "SIDES": pick({"vegetable", "starch", "fruit"}, N_SIDES),
            "STOCK-UP": pick({"pantry_staple", "condiment"}, N_STAPLES, need_compare=True)}


def tray_row(b: dict) -> str:
    unit = "/lb" if b["_basis"] == "lb" else ""
    cmp_ = (f"cheapest of {b['_stores']} stores (median ${b['_median']:.2f}{unit})"
            if b["_stores"] >= 2 else "only store with it")
    return f"{deal_row(b, 70)} | item: {b['_item']}; {cmp_}"


def run(model: str, scenario: Scenario, seed: int) -> dict:
    labels = json.loads(Path(scenario.snapshot).with_suffix(".labels.json").read_text())["labels"]
    trays = build_trays(scenario.deals(), labels)
    blocks, presented = [], []
    for name, rows in trays.items():
        blocks.append(f"**{name}** ({len(rows)}):\n" + "\n".join(tray_row(b) for b in rows))
        presented += [b["deal_id"] for b in rows]
    user = (f"**User requirements:**\n{scenario.describe()}\n\n"
            "Rows: id | name | price | category | store | comparison\n\n" + "\n\n".join(blocks)
            + "\n\n" + PLAN_CONTRACT.replace("{num_meals}", str(scenario.num_meals)))
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    local = model.startswith("ollama:")
    reply = chat(model, messages, json_mode=True, seed=seed, max_tokens=3000 if local else 12000)
    return {"plan": parse_json(reply.text),
            "transcript": messages + [{"role": "assistant", "content": reply.text}],
            "calls": [{"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens,
                       "seconds": round(reply.seconds, 1), "finish_reason": reply.finish_reason,
                       "truncated_prompt": reply.truncated_prompt, "num_ctx": reply.num_ctx,
                       "tool_calls": []}],
            "presented_ids": presented}


if __name__ == "__main__":  # print the trays for inspection
    import sys
    snap = sys.argv[1]
    sc = Scenario(snapshot=snap)
    labs = json.loads(Path(snap).with_suffix(".labels.json").read_text())["labels"]
    for name, rows in build_trays(sc.deals(), labs).items():
        print(f"\n{name}")
        for b in rows:
            print(" ", tray_row(b))
