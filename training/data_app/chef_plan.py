"""Programmatic ``chef_plan`` examples.

The prompt shows the chef ALL deals (including chicken/beef/salmon) and the
completion is a ChefPlan that groups only the dietary-compliant ones into three
complementary protein+starch+vegetable sets — the exact signal the chef gets
wrong today (qwen2.5:7b puts meat in a vegetarian's groups). The rationale
states the exclusion explicitly so the model learns to justify it.

Completions are built as plain dicts and returned to the orchestrator, which
validates them through the real ``ChefPlan`` schema (3 non-empty groups) before
writing — this module asserts non-emptiness too so a bad subset is skipped, not
silently emitted.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict

from app.agents.prompts import PromptTemplates

from .example import Example
from .seed_catalog import (
    PROFILES,
    PROTEIN_CATEGORIES,
    categorize,
    forbidden_terms_for,
    is_compliant,
    quantity_estimate,
)


def _selection(deal: dict) -> dict:
    return {
        "product_name": deal["product_name"],
        "quantity_estimate": quantity_estimate(categorize(deal["product_name"])),
        "deal_id": deal["deal_id"],
    }


def _build_groups(compliant: list[dict], rng: random.Random, n_groups: int = 3):
    """Round-robin compliant deals into n complementary groups; pantry staples
    are shared across all groups to create ingredient reuse."""
    deals = list(compliant)
    rng.shuffle(deals)

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for d in deals:
        by_cat[categorize(d["product_name"])].append(d)

    proteins = [d for cat in PROTEIN_CATEGORIES for d in by_cat.get(cat, [])]
    starches = by_cat.get("starch", [])
    veg = by_cat.get("vegetable", []) + by_cat.get("fruit", []) + by_cat.get("other", [])
    pantry = by_cat.get("pantry", [])

    groups: list[list[dict]] = [[] for _ in range(n_groups)]
    # Distribute proteins first so every group gets one (drives complementarity).
    for pool in (proteins, starches, veg):
        for i, d in enumerate(pool):
            groups[i % n_groups].append(d)
    # Shared pantry staples -> reuse across all groups.
    for g in groups:
        g.extend(pantry)

    return groups, pantry


def selection_groups(deals: list[dict], restrictions: list[str], rng: random.Random) -> list[list[dict]]:
    """Compliant deals grouped into 3 selection-dict groups — the shape a
    SousChef receives as its assigned ``ingredient_group`` (reused by the
    sous_chef distillation module so its inputs match the chef's real output)."""
    compliant = [d for d in deals if is_compliant(d["product_name"], restrictions)]
    groups, _shared = _build_groups(compliant, rng)
    return [[_selection(d) for d in g] for g in groups]


def _reuse_map(groups: list[list[dict]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for g in groups:
        for d in g:
            counts[d["product_name"]] += 1
    return dict(counts)


def _rationale(restrictions: list[str], excluded: list[str], shared: list[dict]) -> str:
    shared_names = ", ".join(d["product_name"] for d in shared) or "none"
    excl = ", ".join(excluded) or "none"
    return (
        f"Grouped dietary-compliant deals into 3 complementary "
        f"protein+starch+vegetable sets, balancing cost and reusing pantry "
        f"staples ({shared_names}) across all groups. Excluded {excl} to respect "
        f"the {', '.join(restrictions)} restriction."
    )


def build_examples(deals: list[dict], seed: int = 42, per_profile: int = 6) -> list[Example]:
    examples: list[Example] = []
    for p_idx, profile in enumerate(PROFILES):
        restrictions = profile["restrictions"]
        forbidden = forbidden_terms_for(restrictions)
        compliant = [d for d in deals if is_compliant(d["product_name"], restrictions)]
        excluded = [d["product_name"] for d in deals if not is_compliant(d["product_name"], restrictions)]

        seen: set[str] = set()
        for k in range(per_profile):
            rng = random.Random(seed + p_idx * 1000 + k)
            groups, shared = _build_groups(compliant, rng)
            if any(not g for g in groups) or len(groups) != 3:
                continue  # subset too small for 3 non-empty groups; skip

            completion_obj = {
                "ingredient_groups": [[_selection(d) for d in g] for g in groups],
                "ingredient_reuse_map": _reuse_map(groups),
                "rationale": _rationale(restrictions, excluded, shared),
            }
            completion = json.dumps(completion_obj, indent=2, ensure_ascii=False)
            if completion in seen:
                continue
            seen.add(completion)

            # Render the prompt exactly as ChefOrchestrator.plan_ingredient_groups does.
            prompt = PromptTemplates.CHEF_INGREDIENT_PLANNING.format(
                deals_json=json.dumps(deals[:100], indent=2),
                budget=profile["budget"],
                household_size=profile["household_size"],
                num_meals=profile["num_meals"],
                dietary_restrictions=", ".join(restrictions),
                preferences=json.dumps({}),
            )
            examples.append(Example(
                task="chef_plan",
                prompt=prompt,
                completion=completion,
                meta={
                    "source": "programmatic",
                    "restrictions": restrictions,
                    "forbidden_terms": forbidden,
                    "excluded_products": excluded,
                },
            ))
    return examples
