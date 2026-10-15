#!/usr/bin/env python
"""Run infer_price_is_per_item over every (unit, sale_story, pre_price_text)
triple this probe actually ingested, and show where it decides vs gives up.

Also lists the distinct unit spellings, so a new one is visible rather than
silently falling through to None.
"""
from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import db
from app.services.products import (
    _PER_ITEM_UNITS,
    _QTY_PREFIX_RE,
    infer_price_is_per_item,
)

CUT = "TIMESTAMPTZ '2026-10-15 00:30+00'"

with db.get_cursor() as cursor:
    cursor.execute(
        f"""
        SELECT product_name, unit, sale_story, pre_price_text, sale_price
        FROM deals WHERE created_at >= {CUT}
        ORDER BY product_name
        """
    )
    rows = [dict(r) for r in cursor.fetchall()]

print(f"{len(rows)} deal row(s) from this probe\n")

verdicts = Counter()
undecided_with_signal = []
for r in rows:
    v = infer_price_is_per_item(r["unit"], r["sale_story"], r["pre_price_text"])
    verdicts[v] += 1
    r["verdict"] = v

print("infer_price_is_per_item over the probe's deals:")
for v in (True, False, None):
    print(f"  {str(v):<6} {verdicts[v]:>4}")

print("\n--- distinct unit spellings, and whether they are recognised ---")
units = Counter((r["unit"] or "<null>") for r in rows)
for u, n in units.most_common():
    norm = u.strip().lower().lstrip("/")
    known = "KNOWN" if norm in _PER_ITEM_UNITS else "unrecognised"
    print(f"  {n:>3}x {u!r:<24} -> {known}")

print("\n--- distinct pre_price_text, and whether the qty-prefix rule fires ---")
pres = Counter((r["pre_price_text"] or "<null>") for r in rows)
for p, n in pres.most_common():
    hits = []
    if _QTY_PREFIX_RE.match(p.strip()):
        hits.append("QTY-PREFIX->False")
    import re as _re
    if _re.search(r"\bfrom\b", p, _re.IGNORECASE):
        hits.append("FROM->False")
    print(f"  {n:>3}x {p!r:<28} {' '.join(hits) or '(no rule fires)'}")

print("\n--- distinct sale_story, and whether a multibuy/bogo rule fires ---")
stories = Counter((r["sale_story"] or "<null>") for r in rows)
from app.services.products import _BOGO_RE, _MULTIBUY_RE
for s, n in stories.most_common():
    hits = []
    if _MULTIBUY_RE.search(s):
        hits.append("MULTIBUY->False")
    if _BOGO_RE.search(s):
        hits.append("BOGO->False")
    print(f"  {n:>3}x {s[:60]!r:<64} {' '.join(hits) or '(no rule fires)'}")

print("\n--- rows where a human can tell but the code cannot (verdict None) ---")
for r in rows:
    if r["verdict"] is not None:
        continue
    u, p, s = (r["unit"] or ""), (r["pre_price_text"] or ""), (r["sale_story"] or "")
    # Hand-picked shapes that clearly carry a quantity or a floor.
    telling = any(x in p.lower() for x in ("2 for", "3 for", "starting at", "from")) \
        or any(x in s.lower() for x in ("mix & match", "when you buy")) \
        or "ea" in u.lower() or "lb" in u.lower() or "kg" in u.lower() \
        or "pkg" in u.lower()
    if telling:
        print(f"  ${r['sale_price']:>7} unit={u!r:<22} pre={p!r:<24} "
              f"story={s[:34]!r}")
        print(f"            {r['product_name'][:88]}")
