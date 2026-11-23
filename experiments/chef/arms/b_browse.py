"""Arm B: the Chef browses the deals by category with tools instead of reading one long list.

Owner's design (2026-11-23): not per-ingredient search ("find potatoes",
"find pasta") but category browsing -- ``list_deal_types()`` then
``list_deals_by_type(category, page)``. The snapshot is already passed through
the dietary gate, so every tool only ever returns deals the user may eat.

Within a category, rows are sorted by name so like items sit together (all the
chicken, all the potatoes), which is what makes comparing across stores
possible. Pages are small enough that nothing is lost in the middle.
"""
from __future__ import annotations

import collections
import json

from experiments.chef.common import PLAN_CONTRACT, Scenario, deal_row
from experiments.chef.llm import chat, parse_json

NAME = "B_browse"
PAGE_SIZE = 40
MAX_TOOL_CALLS = 15
OLLAMA_CTX = 32768

SYSTEM = """You are a home cook planning a week of meals from this week's grocery flyers near the user.
You cannot see the flyers directly: browse them with the tools. Start with list_deal_types, then
open the categories you need. Plan the way an experienced bargain-hunting cook does:
1. Find the anchors: the genuinely good protein and main-component deals worth building meals around
   (compare the same item across stores).
2. Find discounted pantry staples worth stocking up on.
3. Pair each anchor with sensible sides that are also on sale.
Only real meals from real cooking ingredients. You have at most {max_calls} tool calls; when you have
enough, stop calling tools and reply with the final plan."""

TOOLS = [
    {"type": "function", "function": {
        "name": "list_deal_types",
        "description": "Deal categories this week with how many deals each has. Already filtered to the user's diet.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "list_deals_by_type",
        "description": f"One page ({PAGE_SIZE} rows) of a category's deals, sorted by name. "
                       "Each row: id | name | price | category | store.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "description": "Exactly as returned by list_deal_types"},
            "page": {"type": "integer", "description": "1-based page number", "default": 1}},
            "required": ["category"]}}},
]


class Catalog:
    def __init__(self, deals: list[dict]):
        self.by_cat: dict[str, list[dict]] = collections.defaultdict(list)
        for d in deals:
            self.by_cat[d.get("category") or "Other"].append(d)
        for rows in self.by_cat.values():
            rows.sort(key=lambda d: (" ".join(d["product_name"].lower().split()), d["deal_id"]))
        self.seen: list[int] = []  # deal ids in the order the model was shown them

    def call(self, name: str, args: dict) -> str:
        if name == "list_deal_types":
            return "\n".join(f"{c}: {len(r)} deals ({-(-len(r) // PAGE_SIZE)} pages)"
                             for c, r in sorted(self.by_cat.items(), key=lambda kv: -len(kv[1])))
        if name == "list_deals_by_type":
            cat = str(args.get("category", ""))
            match = next((c for c in self.by_cat if c.lower() == cat.strip().lower()), None)
            if match is None:
                return f"Unknown category {cat!r}. Valid: {', '.join(sorted(self.by_cat))}"
            rows = self.by_cat[match]
            pages = -(-len(rows) // PAGE_SIZE)
            try:
                page = max(1, int(args.get("page") or 1))
            except (TypeError, ValueError):
                page = 1
            chunk = rows[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
            self.seen += [d["deal_id"] for d in chunk if d["deal_id"] not in self.seen]
            body = "\n".join(deal_row(d) for d in chunk) or "(no rows on this page)"
            return f"{match} — page {page} of {pages}:\n{body}"
        return f"Unknown tool {name!r}"


def run(model: str, scenario: Scenario, seed: int) -> dict:
    cat = Catalog(scenario.deals())
    user = (f"**User requirements:**\n{scenario.describe()}\n\n"
            + PLAN_CONTRACT.replace("{num_meals}", str(scenario.num_meals)))
    messages = [{"role": "system", "content": SYSTEM.format(max_calls=MAX_TOOL_CALLS)},
                {"role": "user", "content": user}]
    local = model.startswith("ollama:")
    calls_meta, tool_log, n_tool_calls, plan = [], [], 0, None
    for _turn in range(MAX_TOOL_CALLS + 2):
        allow_tools = n_tool_calls < MAX_TOOL_CALLS
        if not allow_tools:
            messages.append({"role": "user", "content":
                             "Tool budget used up. Reply now with ONLY the final plan JSON."})
        reply = chat(model, messages, tools=TOOLS if allow_tools else None,
                     json_mode=not allow_tools, seed=seed,
                     max_tokens=3000 if local else 12000,
                     num_ctx=OLLAMA_CTX if local else None)
        calls_meta.append(_call_meta(reply))
        if not reply.tool_calls:
            messages.append({"role": "assistant", "content": reply.text})
            plan = parse_json(reply.text)
            if plan is None:  # one format repair: planned fine, answered in prose
                messages.append({"role": "user", "content":
                                 "Now output that plan as ONLY the JSON object in the required shape."})
                reply = chat(model, messages, json_mode=True, seed=seed,
                             max_tokens=3000 if local else 12000,
                             num_ctx=OLLAMA_CTX if local else None)
                calls_meta.append(_call_meta(reply))
                messages.append({"role": "assistant", "content": reply.text})
                plan = parse_json(reply.text)
            break
        messages.append({"role": "assistant", "content": reply.text or "", "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
            for c in reply.tool_calls]})
        for c in reply.tool_calls:
            n_tool_calls += 1
            out = cat.call(c["name"], c["arguments"] if isinstance(c["arguments"], dict) else {})
            tool_log.append({"name": c["name"], "arguments": c["arguments"],
                             "result_head": out.split("\n", 1)[0]})
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    return {"plan": plan, "transcript": messages, "calls": calls_meta,
            "presented_ids": cat.seen, "tool_log": tool_log}


def _call_meta(r) -> dict:
    return {"input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "seconds": round(r.seconds, 1), "finish_reason": r.finish_reason,
            "truncated_prompt": r.truncated_prompt, "num_ctx": r.num_ctx,
            "tool_calls": [c["name"] for c in r.tool_calls]}
