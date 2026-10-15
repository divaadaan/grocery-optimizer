#!/usr/bin/env python
"""The probe's findings: oracle disagreements, multi-product rate, allergens."""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from app.agents import dietary
from app.db.database import db
from app.services import products as P
from app.tests.test_multiproduct_lineitems import SIZE_TOKEN, looks_multiproduct
from names import selected

ROWS = selected()
LABELS = json.load(open("/tmp/probe_labelled.json", encoding="utf-8"))["labels"]
BY_RAW = {e["raw_name"]: e for e in LABELS}

print("=" * 78)
print("1. ALLERGEN-RELEVANT LABELS")
print("=" * 78)

with db.get_cursor() as cursor:
    cursor.execute(
        """
        SELECT product_id, canonical_name, category, status,
               contains_meat, contains_fish, contains_dairy,
               contains_egg, contains_nuts, is_pantry_staple, proposal_reason
        FROM products WHERE status = 'proposed' ORDER BY canonical_name
        """
    )
    proposed = [dict(r) for r in cursor.fetchall()]

print(f"{len(proposed)} product(s) proposed by this probe\n")
for attr in P.ATTRIBUTES:
    hits = [p["canonical_name"] for p in proposed if p[attr]]
    print(f"  {attr:<16} {len(hits):>2}  {', '.join(hits)}")

print("\n  deals reachable through each allergen-bearing product:")
with db.get_cursor() as cursor:
    cursor.execute(
        """
        SELECT p.canonical_name, p.contains_nuts, p.contains_fish,
               count(DISTINCT d.deal_id) AS deals
        FROM products p
        JOIN product_aliases a ON a.product_id = p.product_id
        JOIN deals d ON d.product_name = a.raw_example
        WHERE p.status = 'proposed' AND (p.contains_nuts OR p.contains_fish)
        GROUP BY 1, 2, 3 ORDER BY 4 DESC
        """
    )
    for r in cursor.fetchall():
        kinds = []
        if r["contains_nuts"]:
            kinds.append("nuts")
        if r["contains_fish"]:
            kinds.append("fish")
        print(f"    {r['canonical_name']:<32} {'+'.join(kinds):<10} "
              f"{r['deals']} deal(s)")

print()
print("=" * 78)
print("2. ORACLE DISAGREEMENTS  (products.oracle_disagreement)")
print("=" * 78)
print("by_attrs = curated columns; by_oracle = dietary.py casefold substring.")
print("Case A = oracle FALSE POSITIVE (attrs safe, oracle says unsafe)")
print("Case B = oracle FALSE NEGATIVE (attrs unsafe, oracle says safe) "
      "<-- dangerous\n")

case_a, case_b = [], []
for p in proposed:
    labels = P.oracle_disagreement(p)
    if not labels:
        continue
    for label in labels:
        by_attrs = P.product_allows(p, [label])
        by_oracle = dietary.is_compliant(p["canonical_name"], [label])
        # Which forbidden substring tripped the oracle, if any.
        name = p["canonical_name"].casefold()
        tripped = [t for t in dietary.forbidden_terms_for([label]) if t in name]
        rec = (p["canonical_name"], label, by_attrs, by_oracle, tripped)
        (case_a if by_attrs and not by_oracle else case_b).append(rec)

def show(bucket, title):
    print(f"--- {title}: {len(bucket)} disagreement(s) ---")
    by_name: dict[str, list] = {}
    for name, label, a, o, t in bucket:
        by_name.setdefault(name, []).append((label, t))
    for name in sorted(by_name):
        entries = by_name[name]
        labs = ", ".join(l for l, _ in entries)
        trip = sorted({x for _, ts in entries for x in ts})
        print(f"  {name}")
        print(f"      labels: {labs}")
        if trip:
            print(f"      oracle tripped on substring(s): {trip}")
        else:
            print(f"      oracle saw no forbidden substring at all")
    print()

show(case_a, "CASE A  oracle false positive")
show(case_b, "CASE B  oracle false negative")

nut_b = [r for r in case_b if r[1] == "no_nuts"]
print(f"CASE B on the ALLERGEN path (no_nuts): {len(nut_b)}")
for name, label, a, o, t in nut_b:
    print(f"  {name}  -- attributes say contains_nuts, oracle sees no nut word")

print(f"\ntotals: {len(case_a)} Case A, {len(case_b)} Case B, "
      f"{len(case_a) + len(case_b)} disagreements over "
      f"{len(proposed)} proposed products")
print(f"distinct products in disagreement: "
      f"{len({r[0] for r in case_a + case_b})}")

print()
print("=" * 78)
print("3. MULTI-PRODUCT TILES")
print("=" * 78)

names = [r["product_name"] for r in ROWS]
flagged = [n for n in names if looks_multiproduct(n)]
sized = [n for n in names if len(SIZE_TOKEN.findall(n)) >= 2]
labelled_multi = {e["raw_name"] for e in LABELS if len(e.get("products", [])) > 1}

print(f"new names in this probe            : {len(names)}")
print(f"detector flags (looks_multiproduct): {len(flagged)} "
      f"({len(flagged) / len(names):.1%})   [baseline on 2 terms: 31%]")
print(f"  of those, >=2 size tokens        : {len(sized)} "
      f"({len(sized) / len(names):.1%})")
print(f"I LABELLED as truly multi-product  : {len(labelled_multi)} "
      f"({len(labelled_multi) / len(names):.1%})")

fp = sorted(set(flagged) - labelled_multi)
fn = sorted(labelled_multi - set(flagged))
print(f"\ndetector FALSE POSITIVES (flagged, actually one product): {len(fp)}")
for n in fp:
    print(f"    {n[:92]}")
print(f"\ndetector MISSES (truly multi-product, not flagged): {len(fn)}")
for n in fn:
    print(f"    {n[:92]}")
tp = len(set(flagged) & labelled_multi)
if flagged:
    print(f"\nprecision {tp / len(flagged):.1%}   "
          f"recall {tp / len(labelled_multi):.1%}")

# Size-token claim from the test file: ">=2 size tokens had no false positives".
sized_fp = [n for n in sized if n not in labelled_multi]
print(f"\n>=2 size tokens, but ONE product (the test file claims zero such): "
      f"{len(sized_fp)}")
for n in sized_fp:
    print(f"    {n[:92]}")

print()
print("=" * 78)
print("4. NAMES PER TERM  (re-projecting the full 50)")
print("=" * 78)
per_term = json.load(open(os.path.join(HERE, "per_term.json"), encoding="utf-8"))
keys_union = set()
tot = 0
for term, v in per_term.items():
    tot += v["distinct_raw_names"]
    print(f"  {term:<16} raw_items={v['raw_items']:<4} "
          f"deals={v['deals']:<4} distinct_names={v['distinct_raw_names']:<4} "
          f"normalized_keys={v['distinct_norm_keys']}")
print(f"\n  total distinct names across 5 terms : {tot}")
print(f"  mean per term                       : {tot / 5:.1f}")
med = sorted(v["distinct_raw_names"] for v in per_term.values())[2]
print(f"  median per term                     : {med}")
print(f"  min / max                           : "
      f"{min(v['distinct_raw_names'] for v in per_term.values())} / "
      f"{max(v['distinct_raw_names'] for v in per_term.values())}")
print(f"\n  projection to 50 terms at the MEAN  : {tot / 5 * 50:.0f} names")
print(f"  projection to 50 terms at the MEDIAN: {med * 50:.0f} names")
print(f"  (prior estimate from 2 terms: ~45/term -> ~2,000)")

print()
print("=" * 78)
print("5. PRICE SEMANTICS AFTER reinfer")
print("=" * 78)
with db.get_cursor() as cursor:
    cursor.execute(
        """
        SELECT price_is_per_item, count(*) AS n
        FROM product_aliases WHERE status = 'proposed'
        GROUP BY 1 ORDER BY 1 NULLS LAST
        """
    )
    for r in cursor.fetchall():
        print(f"  price_is_per_item = {str(r['price_is_per_item']):<6} "
              f"{r['n']:>4} proposed alias(es)")
    cursor.execute(
        """
        SELECT count(*) AS n FROM product_aliases
        WHERE status = 'proposed' AND price_is_per_item IS NULL
        """
    )
    blocked = cursor.fetchone()["n"]
    cursor.execute("SELECT count(*) AS n FROM product_aliases "
                   "WHERE status = 'proposed'")
    total = cursor.fetchone()["n"]
print(f"\n  {blocked} of {total} proposed aliases ({blocked / total:.0%}) would "
      f"be BLOCKED from linking\n  by the NOT NULL price_is_per_item guard, "
      f"even after a human confirms them.")

print()
print("=" * 78)
print("6. THE unmapped_deal_names VIEW vs link_deals")
print("=" * 78)
with db.get_cursor() as cursor:
    cursor.execute(
        """
        SELECT d.product_name, count(DISTINCT dp.product_id) AS products
        FROM deals d
        JOIN deal_products dp ON dp.deal_id = d.deal_id
        WHERE d.product_id IS NULL
        GROUP BY 1 ORDER BY 2 DESC, 1
        """
    )
    rows = [dict(r) for r in cursor.fetchall()]
print(f"{len(rows)} name(s) are fully linked through deal_products yet still")
print("appear in unmapped_deal_names, because the view keys off the legacy")
print("deals.product_id, which link_deals deliberately leaves NULL for a")
print("multi-product tile. They will sit in the review queue forever.")
for r in rows[:15]:
    print(f"    {r['products']} product(s)  {r['product_name'][:76]}")
