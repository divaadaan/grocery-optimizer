#!/usr/bin/env python
"""Exact names-per-term and cross-term overlap for the 5 probe terms.

The DB cannot answer this: deals.category is derived from the search term, and
"peanut butter" and "tofu" are both Pantry, so they collapse into one bucket.
So re-collect per term. Network only, no writes. One shared client, so the
1.5s inter-request interval still applies.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app.services.flipp_client import FlippSearchClient
from app.services.flipp_ingestion import collect_deals
from app.services.products import normalize_name

TERMS = ["peanut butter", "tomatoes", "bread", "canned tuna", "tofu"]
POSTAL = "M5V3A8"

client = FlippSearchClient()
per_term: dict[str, dict] = {}

for term in TERMS:
    deals, report = collect_deals(client, POSTAL, terms=[term])
    raw_names = sorted({d.product_name for d in deals})
    norm_names = sorted({normalize_name(d.product_name) for d in deals})
    per_term[term] = {
        "raw_items": report.raw_items,
        "normalized_rows": report.normalized,
        "skipped_no_name": report.skipped_no_name,
        "skipped_no_price": report.skipped_no_price,
        "duplicate_offers": report.duplicate_offers,
        "deals": len(deals),
        "distinct_raw_names": len(raw_names),
        "distinct_norm_keys": len(norm_names),
        "names": raw_names,
    }
    print(f"{term:<16} raw={report.raw_items:<4} norm={report.normalized:<4} "
          f"deals={len(deals):<4} distinct_names={len(raw_names):<4} "
          f"distinct_keys={len(norm_names)}")

print()
total_gross = sum(v["distinct_raw_names"] for v in per_term.values())
union = set()
for v in per_term.values():
    union |= set(v["names"])
print(f"gross sum of per-term distinct names : {total_gross}")
print(f"union across the 5 terms             : {len(union)}")
print(f"cross-term duplication               : {total_gross - len(union)} "
      f"({(total_gross - len(union)) / total_gross:.1%})")
print(f"mean distinct names per term (gross) : {total_gross / len(TERMS):.1f}")
print(f"mean NEW names per term (union/5)    : {len(union) / len(TERMS):.1f}")

# Pairwise overlap, so the projection can account for term adjacency.
print("\npairwise shared names:")
for i, a in enumerate(TERMS):
    for b in TERMS[i + 1:]:
        shared = set(per_term[a]["names"]) & set(per_term[b]["names"])
        if shared:
            print(f"  {a!r} & {b!r}: {len(shared)}")
            for s in sorted(shared)[:6]:
                print(f"      {s}")

with open(os.path.join(HERE, "per_term.json"), "w", encoding="utf-8") as fh:
    json.dump(per_term, fh, indent=1, ensure_ascii=False)
print(f"\nwrote {os.path.join(HERE, 'per_term.json')}")
