"""Distilled ``sous_chef_recipe`` examples.

Recipe prose is the one task we don't label by rule — we distill it from a
strong teacher (``qwen2.5:7b`` via Ollama, the project's chef model) prompted
with the app's real SOUS_CHEF_RECIPE_GENERATION template, then keep only
teacher outputs that (a) validate through ``RecipeBatch`` and (b) pass a
forbidden-term rule check for the profile's restrictions. The completion is
re-serialized from the validated recipes as the bare JSON array the prompt asks
for, so it is canonical and schema-clean regardless of teacher quirks.

Needs an Ollama instance reachable at ``--base-url`` / ``$OLLAMA_BASE_URL``
holding the teacher model. On this host that is the network-exposed Windows
Ollama at the WSL gateway IP (see training/README.md and the dual-instance
quirk) — NOT loopback. If unreachable, the orchestrator skips this task with a
warning rather than failing the whole build.
"""

from __future__ import annotations

import json
import random

import requests

from app.agents.llm_output import RecipeBatch
from app.agents.prompts import PromptTemplates

from .chef_plan import selection_groups
from .example import Example
from .seed_catalog import PROFILES, violating_terms


class TeacherUnavailable(RuntimeError):
    """Ollama / the teacher model could not be reached."""


def check_teacher(base_url: str, model: str, timeout: int = 5) -> None:
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise TeacherUnavailable(f"Ollama not reachable at {base_url}: {e}") from e
    names = {m.get("name", "") for m in resp.json().get("models", [])}
    if model not in names and not any(n.startswith(model.split(":")[0]) for n in names):
        raise TeacherUnavailable(
            f"teacher model {model!r} not found at {base_url} (have: {sorted(names)})"
        )


def _call_teacher(base_url: str, model: str, prompt: str, timeout: int, seed: int) -> str:
    resp = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.7, "seed": seed},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


def _compliant_recipes(batch: RecipeBatch, restrictions: list[str]) -> list[dict]:
    """Drop any recipe whose ingredients trip a forbidden term; return the rest
    as clean dicts. The teacher is strong but not trusted on dietary compliance —
    that's the failure mode we're fixing, so we never let a violation through."""
    kept = []
    for recipe in batch.recipes:
        if any(violating_terms(ing.name, restrictions) for ing in recipe.ingredients):
            continue
        kept.append(recipe.model_dump())
    return kept


def build_examples(
    deals: list[dict],
    base_url: str,
    model: str = "qwen2.5:7b",
    seed: int = 42,
    per_group: int = 2,
    timeout: int = 180,
) -> list[Example]:
    """Distill SousChef recipes for each compliant ingredient group of each
    profile. Raises TeacherUnavailable if the teacher can't be reached (the
    orchestrator decides whether to skip)."""
    check_teacher(base_url, model, timeout=min(timeout, 10))

    examples: list[Example] = []
    for p_idx, profile in enumerate(PROFILES):
        restrictions = profile["restrictions"]
        rng = random.Random(seed + p_idx * 3000)
        groups = selection_groups(deals, restrictions, rng)

        for g_idx, group in enumerate(groups):
            target = min(2, len(group)) or 1
            prompt = PromptTemplates.SOUS_CHEF_RECIPE_GENERATION.format(
                ingredients_json=json.dumps(group, indent=2),
                target_recipe_count=target,
                servings=profile["household_size"],
                dietary_restrictions=", ".join(restrictions),
            )
            for k in range(per_group):
                call_seed = seed + p_idx * 3000 + g_idx * 100 + k
                try:
                    raw = _call_teacher(base_url, model, prompt, timeout, call_seed)
                    batch = RecipeBatch.model_validate(json.loads(raw))
                except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
                    print(f"  [sous_chef] p{p_idx} g{g_idx} k{k} dropped: {type(e).__name__}: {str(e)[:120]}")
                    continue

                recipes = _compliant_recipes(batch, restrictions)
                if not recipes:
                    print(f"  [sous_chef] p{p_idx} g{g_idx} k{k} dropped: all recipes tripped forbidden terms")
                    continue

                completion = json.dumps(recipes, indent=2, ensure_ascii=False)
                examples.append(Example(
                    task="sous_chef_recipe",
                    prompt=prompt,
                    completion=completion,
                    meta={"source": f"distilled:{model}", "restrictions": restrictions,
                          "group_index": g_idx, "num_recipes": len(recipes)},
                ))
    return examples
