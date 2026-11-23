"""Arm A (control): every gated deal in one prompt, one compact row each, nothing truncated.

Answers: once the Chef can actually see the whole list (the current pipeline
can't -- 2026-11-23 trace), is a single long prompt good enough?
"""
from __future__ import annotations

import random

from experiments.chef.common import PLAN_CONTRACT, Scenario, deal_row
from experiments.chef.llm import chat, parse_json

NAME = "A_full_list"

SYSTEM = """You are a home cook planning a week of meals from this week's grocery flyers.
Plan the way an experienced bargain-hunting cook does:
1. Find the loss leaders: the big, genuinely cheap items (proteins and main components) worth building meals around.
2. Find discounted pantry staples worth stocking up on for future weeks.
3. Pair each anchor with sensible sides that are also on sale.
Only real meals. Ignore items that are not cooking ingredients for these meals."""


def run(model: str, scenario: Scenario, seed: int) -> dict:
    deals = scenario.deals()
    random.Random(seed).shuffle(deals)  # per-seed order: exposes position bias
    rows = "\n".join(deal_row(d) for d in deals)
    user = (f"**User requirements:**\n{scenario.describe()}\n\n"
            f"**This week's deals** ({len(deals)} rows: id | name | price | category | store):\n{rows}\n\n"
            + PLAN_CONTRACT.replace("{num_meals}", str(scenario.num_meals)))
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    # Remote reasoning models spend output tokens thinking; local num_predict must fit num_ctx.
    max_tokens = 3000 if model.startswith("ollama:") else 12000
    reply = chat(model, messages, json_mode=True, seed=seed, max_tokens=max_tokens)
    return {
        "plan": parse_json(reply.text),
        "transcript": messages + [{"role": "assistant", "content": reply.text}],
        "calls": [_call_meta(reply)],
        "presented_ids": [d["deal_id"] for d in deals],
    }


def _call_meta(r) -> dict:
    return {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "seconds": round(r.seconds, 1), "finish_reason": r.finish_reason,
            "truncated_prompt": r.truncated_prompt, "num_ctx": r.num_ctx,
            "tool_calls": [c["name"] for c in r.tool_calls]}
