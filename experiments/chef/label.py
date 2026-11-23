"""Label every deal in a snapshot with an item identity and a culinary role (arm C's input).

    .venv/bin/python -m experiments.chef.label --snapshot <snapshot.json> [--model hf:openai/gpt-oss-120b]

Writes ``<snapshot>.labels.json``: {deal_id: {"item": "...", "role": "..."}}.

EXPERIMENT ONLY. In production this would be a *proposed* curated product
attribute, confirmed in product review -- the same draft -> approve flow as the
dietary attributes. It never feeds a dietary decision: the snapshot is already
gated, so the labels can only shape which permitted deals are surfaced.

Batches of 40 rows keep every deal near the top of its prompt (no lost-in-the-middle).
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from experiments.chef.common import deal_row
from experiments.chef.llm import chat, parse_json

ROLES = ["protein", "vegetable", "fruit", "starch", "dairy", "pantry_staple", "condiment",
         "prepared_meal", "snack", "beverage", "non_food"]
BATCH = 40

PROMPT = """Label each grocery deal below for meal planning.
For each row give:
- "item": the generic grocery item a cook would shop for, lowercase, singular-ish, no brand or size
  (e.g. "chicken thighs", "whole chicken", "ground beef", "russet potatoes", "broccoli", "dry pasta",
  "pasta sauce", "olive oil", "cheddar cheese"). Use the SAME item string for the same thing across
  stores so they can be compared. If a row offers several different items ("X or Y"), name the first.
- "role": one of {roles}.
  protein = raw/fresh meat, poultry, fish, seafood, eggs, tofu, dried/canned legumes.
  prepared_meal = ready-to-eat or heat-and-serve dishes. snack = chips, cookies, candy, bars.
  pantry_staple = shelf-stable cooking basics (pasta, rice, flour, oil, canned tomatoes, stock).
Return ONLY JSON: {{"labels": [{{"id": 123, "item": "...", "role": "..."}}, ...]}} with one entry per row.

Rows (id | name | price | category | store):
{rows}"""


def label_batch(model: str, deals: list[dict]) -> dict:
    rows = "\n".join(deal_row(d) for d in deals)
    for attempt in range(3):
        r = chat(model, [{"role": "user", "content": PROMPT.format(roles=", ".join(ROLES), rows=rows)}],
                 json_mode=True, max_tokens=8000, temperature=0)
        got = parse_json(r.text) or {}
        out = {}
        for x in got.get("labels") or []:
            try:
                out[int(x["id"])] = {"item": str(x["item"]).strip().lower(),
                                     "role": x["role"] if x.get("role") in ROLES else "unknown"}
            except (KeyError, TypeError, ValueError):
                continue
        ids = {d["deal_id"] for d in deals}
        if ids <= set(out):
            return {i: out[i] for i in ids}
    return {i: out[i] for i in ids if i in out}  # partial: missing ids stay unlabelled


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--model", default="hf:openai/gpt-oss-120b")
    a = ap.parse_args()
    deals = json.loads(Path(a.snapshot).read_text())["deals"]
    # Batch within category so neighbours are comparable and item strings stay consistent.
    deals.sort(key=lambda d: (d.get("category") or "", " ".join(d["product_name"].lower().split())))
    batches = [deals[i:i + BATCH] for i in range(0, len(deals), BATCH)]
    labels: dict = {}
    with ThreadPoolExecutor(6) as ex:
        for part in ex.map(lambda b: label_batch(a.model, b), batches):
            labels.update(part)
    out = Path(a.snapshot).with_suffix(".labels.json")
    out.write_text(json.dumps({"model": a.model, "labels": {str(k): v for k, v in sorted(labels.items())}},
                              indent=1))
    missing = len(deals) - len(labels)
    print(out, f"{len(labels)} labelled, {missing} missing")


if __name__ == "__main__":
    main()
