#!/usr/bin/env python
"""Print the labelling worksheet: new names, with price signals and MP flag."""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app.tests.test_multiproduct_lineitems import SIZE_TOKEN, looks_multiproduct

with open(os.path.join(HERE, "unmapped_dump.json"), encoding="utf-8") as fh:
    rows = json.load(fh)

# Only names that were exported for labelling (currently valid) AND new.
with open("/tmp/probe.json", encoding="utf-8") as fh:
    exported = set(json.load(fh)["unmapped"])

which = sys.argv[1] if len(sys.argv) > 1 else "new"
if which == "new":
    sel = [r for r in rows if r["new_this_run"] and r["product_name"] in exported]
elif which == "old":
    sel = [r for r in rows if r["seen_before"] and r["product_name"] in exported]
else:
    sel = [r for r in rows if r["product_name"] in exported]

print(f"{len(sel)} name(s)\n")
for i, r in enumerate(sel, 1):
    mp = "MP" if looks_multiproduct(r["product_name"]) else "  "
    sz = len(SIZE_TOKEN.findall(r["product_name"]))
    u = "|".join(r["units"]) or "-"
    st = "|".join(r["sale_stories"]) or "-"
    pp = "|".join(r["pre_price_texts"]) or "-"
    print(f"{i:>3} [{mp} sz={sz}] {r['product_name']}")
    print(f"      cat={'/'.join(r['categories'])} n={r['deal_count']} "
          f"${r['min_price']}-{r['max_price']} unit={u!r} story={st!r} pre={pp!r}")
