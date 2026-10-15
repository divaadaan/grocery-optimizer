#!/usr/bin/env python
"""How much of the oracle-disagreement set rests on a judgement call?"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import db
from app.services import products as P

with db.get_cursor() as cursor:
    cursor.execute(
        """
        SELECT p.product_id, p.canonical_name, p.category, p.is_pantry_staple,
               p.contains_meat, p.contains_fish, p.contains_dairy,
               p.contains_egg, p.contains_nuts,
               max(a.confidence) AS best_conf, min(a.confidence) AS worst_conf
        FROM products p
        LEFT JOIN product_aliases a ON a.product_id = p.product_id
        WHERE p.status = 'proposed'
        GROUP BY p.product_id, p.canonical_name, p.category, p.is_pantry_staple,
                 p.contains_meat, p.contains_fish, p.contains_dairy,
                 p.contains_egg, p.contains_nuts
        ORDER BY p.canonical_name
        """
    )
    rows = [dict(r) for r in cursor.fetchall()]

dis = [r for r in rows if P.oracle_disagreement(r)]
low = [r for r in dis if (r["best_conf"] or 0) < 0.8]
print(f"products in oracle disagreement        : {len(dis)}")
print(f"  of those, best label confidence <0.8 : {len(low)}")
print("\n  the judgement calls (a reviewer should check these first):")
for r in low:
    on = [a.replace("contains_", "") for a in P.ATTRIBUTES if r[a]]
    print(f"    {r['canonical_name']:<32} conf={float(r['best_conf']):.2f}  "
          f"{','.join(on) or '-'}  {P.oracle_disagreement(r)}")

print("\n  the confident ones (attributes are not in doubt):")
for r in dis:
    if r in low:
        continue
    on = [a.replace("contains_", "") for a in P.ATTRIBUTES if r[a]]
    print(f"    {r['canonical_name']:<32} conf={float(r['best_conf']):.2f}  "
          f"{','.join(on) or '-'}  {P.oracle_disagreement(r)}")

print("\n--- is_pantry_staple spread over the proposed set ---")
staples = [r["canonical_name"] for r in rows if r["is_pantry_staple"]]
print(f"  {len(staples)} of {len(rows)}: {', '.join(sorted(staples))}")
cats: dict[str, int] = {}
for r in rows:
    cats[r["category"]] = cats.get(r["category"], 0) + 1
print("\n--- categories proposed ---")
for c, n in sorted(cats.items(), key=lambda kv: -kv[1]):
    print(f"  {c:<16} {n}")
