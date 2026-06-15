"""Programmatic ``nutritionist_verdict`` examples.

Two deterministically-labelled kinds, one per observed failure mode:

- REJECT: a recipe that contains a forbidden ingredient for the profile
  (e.g. chicken for a vegetarian). Verdict ``approved=false`` with the offending
  ingredients listed in ``dietary_compliance.violations`` and named in the
  feedback. These are the hard regression guards.
- APPROVE: a fully-compliant recipe, deliberately including *minimal* ones (the
  2-ingredient Tomato Basil Pasta control that phi4-mini false-rejects). Verdict
  ``approved=true`` with empty violations — the direct fix for the
  over-strictness / false-rejection mode.

Completions are validated by the orchestrator through the real
``NutritionistVerdict`` schema (which requires ``approved``).
"""

from __future__ import annotations

import json
import random

from app.agents.prompts import PromptTemplates

from .example import Example
from .seed_catalog import (
    PROFILES,
    categorize,
    forbidden_terms_for,
    is_compliant,
    recipe_amount,
    violating_terms,
)

# A compliant, minimal control kept verbatim so the exact recipe phi4-mini
# false-rejects is in the APPROVE set (see fixture control1_compliant_pasta).
TOMATO_BASIL_PASTA = {
    "name": "Tomato Basil Pasta",
    "meal_type": "lunch",
    "cuisine_type": "Italian-American",
    "ingredients": [
        {"name": "Pasta Penne", "quantity": "16 oz", "unit": "oz", "price": 1.49},
        {"name": "Tomato Sauce", "quantity": "14 oz", "unit": "oz", "price": 1.99},
    ],
    "instructions": [
        "Cook pasta penne according to package instructions until al dente, drain and set aside.",
        "In a large skillet over medium heat, warm the tomato sauce for five to six minutes, toss with the pasta and serve.",
    ],
    "estimated_prep_time": 20,
    "total_cost": 3.48,
    "servings": 2,
}


def _ingredient(deal: dict) -> dict:
    qty, unit = recipe_amount(categorize(deal["product_name"]))
    return {"name": deal["product_name"], "quantity": f"{qty} {unit}".strip(),
            "unit": unit, "price": deal["sale_price"]}


def _nutrition_facts(ingredients: list[dict]) -> dict:
    """Rough deterministic estimate — non-empty and plausible; not the training
    signal we care about (the verdict + violations are)."""
    cats = [categorize(i["name"]) for i in ingredients]
    protein = 18 * sum(c in ("protein", "dairy") for c in cats) + 6
    carbs = 30 * sum(c == "starch" for c in cats) + 10
    fat = 8 * sum(c in ("protein", "dairy", "pantry") for c in cats) + 4
    return {
        "calories_per_serving": protein * 4 + carbs * 4 + fat * 9,
        "protein_g": protein, "carbs_g": carbs, "fat_g": fat,
        "fiber_g": 3 * sum(c in ("vegetable", "fruit", "starch") for c in cats),
        "vitamins": ["Vitamin C", "Iron"] if any(c == "vegetable" for c in cats) else ["Iron"],
    }


def _instructions(ingredients: list[dict]) -> list[str]:
    names = [i["name"].lower() for i in ingredients]
    return [
        "Prepare and measure all ingredients.",
        f"Cook the main components ({', '.join(names[:2])}) over medium heat until done.",
        "Combine everything, season with salt and pepper, and serve.",
    ]


def _recipe(recipe_id: str, name: str, ingredients: list[dict], meal_type: str = "dinner") -> dict:
    return {
        "recipe_id": recipe_id,
        "name": name,
        "ingredients": ingredients,
        "instructions": _instructions(ingredients),
        "servings": 2,
        "total_cost": round(sum(i["price"] for i in ingredients), 2),
        "estimated_prep_time": 35,
        "meal_type": meal_type,
        "cuisine_type": "American",
    }


def _verdict(approved: bool, recipe: dict, violations: list[str], restrictions: list[str]) -> dict:
    if approved:
        feedback = (
            f"Approved: every ingredient complies with the {', '.join(restrictions)} "
            "restriction. The recipe is balanced and the instructions are clear and achievable."
        )
        health = 78.0
    else:
        feedback = (
            f"Rejected: contains {', '.join(violations)}, which violate the "
            f"{', '.join(restrictions)} restriction. Replace with a compliant protein "
            "such as tofu, eggs (if not vegan), or beans."
        )
        health = 35.0
    return {
        "approved": approved,
        "feedback": feedback,
        "nutrition_facts": _nutrition_facts(recipe["ingredients"]),
        "dietary_compliance": {
            "allergen_free": True,
            "meets_restrictions": approved,
            "violations": violations,
        },
        "health_score": health,
    }


def _example(recipe: dict, restrictions: list[str], expected: bool) -> Example:
    violations = [i["name"] for i in recipe["ingredients"]
                  if violating_terms(i["name"], restrictions)]
    approved = not violations
    assert approved == expected, f"{recipe['name']}: label/compliance mismatch"
    prompt = PromptTemplates.NUTRITIONIST_VALIDATION.format(
        recipe_json=json.dumps(recipe, indent=2),
        dietary_restrictions=", ".join(restrictions),
    )
    completion = json.dumps(_verdict(approved, recipe, violations, restrictions),
                            indent=2, ensure_ascii=False)
    return Example(
        task="nutritionist_verdict",
        prompt=prompt,
        completion=completion,
        meta={"source": "programmatic", "restrictions": restrictions,
              "expected_approved": approved, "violations": violations},
    )


def build_examples(deals: list[dict], seed: int = 42, per_profile: int = 10) -> list[Example]:
    examples: list[Example] = []
    for p_idx, profile in enumerate(PROFILES):
        restrictions = profile["restrictions"]
        rng = random.Random(seed + p_idx * 2000)

        compliant = [d for d in deals if is_compliant(d["product_name"], restrictions)]
        forbidden_deals = [d for d in deals if not is_compliant(d["product_name"], restrictions)]
        sides = [d for d in compliant if categorize(d["product_name"]) in ("starch", "vegetable", "pantry")]

        n = 0
        # REJECT cases: one forbidden "star" + a couple of compliant sides.
        for star in forbidden_deals:
            if n >= per_profile:
                break
            chosen_sides = rng.sample(sides, k=min(2, len(sides))) if sides else []
            ingredients = [_ingredient(star)] + [_ingredient(s) for s in chosen_sides]
            recipe = _recipe(f"rej-{p_idx}-{n:02d}", f"{star['product_name']} Skillet", ingredients)
            examples.append(_example(recipe, restrictions, expected=False))
            n += 1

        # APPROVE cases: the minimal control (for vegetarian-style profiles where
        # its ingredients are compliant) + compliant combos.
        if is_compliant("Pasta Penne", restrictions) and is_compliant("Tomato Sauce", restrictions):
            examples.append(_example(TOMATO_BASIL_PASTA, restrictions, expected=True))

        proteins = [d for d in compliant if categorize(d["product_name"]) in ("protein", "dairy")]
        n = 0
        while n < per_profile and proteins and sides:
            star = proteins[n % len(proteins)]
            chosen_sides = rng.sample(sides, k=min(2, len(sides)))
            ingredients = [_ingredient(star)] + [_ingredient(s) for s in chosen_sides]
            recipe = _recipe(f"app-{p_idx}-{n:02d}", f"{star['product_name']} Bowl", ingredients)
            examples.append(_example(recipe, restrictions, expected=True))
            n += 1

    return examples
