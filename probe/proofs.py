#!/usr/bin/env python
"""Two code-level claims from the probe, demonstrated rather than asserted.

No DB writes. Reads only.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.product_proposer import proposals_from_payload
from app.services.products import infer_price_is_per_item, normalize_name

print("=" * 78)
print("CLAIM 1: infer_price_is_per_item's documented multi-buy precedence has")
print("a hole. Its docstring says \"'2 FOR $5' with unit='ea.' means two items")
print("for five dollars, not five dollars each -- reading the unit first would")
print("halve every such price.\" That holds only when the quantity is in")
print("sale_story. The provider also renders it as pre_price_text, and")
print("_QTY_PREFIX_RE is anchored to '^N/$', so '2 for' slips through.")
print("=" * 78)
CASES = [
    ("ea.", "2 FOR $5", None, "quantity in sale_story (the documented case)"),
    ("ea.", None, "2/", "quantity as a bare 'N/' prefix (handled since 011)"),
    ("ea.", None, "2 for", "quantity as a '2 for' PREFIX  <-- OBSERVED LIVE"),
    ("each", None, "2 for", "same, spelled 'each'"),
    (None, None, "2 for", "'2 for' with no unit (both live rows look like this)"),
    ("ea.", None, "starting at", "'starting at' floor  <-- OBSERVED LIVE"),
    ("ea.", None, "from", "'from' floor (handled)"),
    ("ea.", "Mix & Match", None, "'Mix & Match' multi-buy idiom, no quantity"),
    ("pkg", None, None, "unit 'pkg'  <-- OBSERVED LIVE, 2 rows"),
    ("pk", None, None, "unit 'pk' (known)"),
    ("ea/lb", None, None, "unit 'ea/lb'  <-- OBSERVED LIVE"),
    ("or $2.89 each", None, None, "unit carrying the per-item PRICE  <-- 13 rows"),
    ("2.87 /ea", None, None, "same shape, no 'or'  <-- OBSERVED LIVE"),
]
print(f"\n  {'unit':<18} {'sale_story':<12} {'pre':<14} {'verdict':<8} note")
for unit, story, pre, note in CASES:
    v = infer_price_is_per_item(unit, story, pre)
    bad = ""
    if pre in ("2 for",) and v is not False:
        bad = "  *** WRONG or UNDECIDED: price covers 2 items"
    if pre == "starting at" and v is not False:
        bad = "  *** UNDECIDED: price is a floor"
    print(f"  {str(unit):<18} {str(story):<12} {str(pre):<14} {str(v):<8} "
          f"{note}{bad}")
print("\n  The dangerous row is unit='ea.' + pre='2 for' -> True. Nothing in")
print("  this probe hit it (the two live '2 for' rows carry no usable unit),")
print("  so it is latent, not realised. The code COMMENT is what is wrong.")

print()
print("=" * 78)
print("CLAIM 2: proposals_from_payload forces price_is_per_item=True for every")
print("single-product entry, on the stated ground that \"a single-product entry")
print("prices per item by definition: there is nothing else on the tile for the")
print("price to be shared with.\" That reasoning does not hold: a SINGLE product")
print("can be sold two-for-$7. And because reinfer_alias_prices only ever fills")
print("NULLs, the wrong True is never corrected.")
print("=" * 78)
payload = {
    "raw_name": "Dempster's sliced breads",   # live: $7.00, pre_price_text '2/'
    "match_existing_product": None,
    "canonical_name": "sliced bread",
    "category": "bakery",
    "contains_meat": False, "contains_fish": False, "contains_dairy": False,
    "contains_egg": False, "contains_nuts": False,
    "is_pantry_staple": False,
    "confidence": 0.8, "reason": "one product", "unmappable": False,
}
auto = proposals_from_payload(dict(payload))[0]
print(f"\n  label omits price_is_per_item -> {auto.price_is_per_item!r}")
print(f"  the deal itself says pre_price_text='2/', sale_price=7.00, i.e. "
      f"2 for $7")
print(f"  infer_price_is_per_item(None, None, '2/') = "
      f"{infer_price_is_per_item(None, None, '2/')!r}")
print("  -> the label's True contradicts the deal text, and reinfer_alias_prices")
print("     will not touch it because it is not NULL.")
explicit = proposals_from_payload({**payload, "price_is_per_item": False})[0]
print(f"\n  passing it explicitly is respected -> {explicit.price_is_per_item!r}")
print("  (this probe set it explicitly on the 9 single-product multi-buy")
print("   names rather than let the default stand)")

print()
print("=" * 78)
print("CLAIM 3: four raw names collapse to one normalized key with CONFLICTING")
print("price semantics, and the alias upsert keeps whichever landed first.")
print("=" * 78)
for raw in ("MUTTI FINELY CHOPPED TOMATOES, 398 mL",
            "Mutti Finely Chopped Tomatoes",
            "Mutti Finely Chopped Tomatoes 398 mL",
            "Mutti Finely Chopped Tomatoes 796 mL"):
    print(f"  {raw:<42} -> {normalize_name(raw)!r}")
print("\n  One of those four is a 2-for-$5 tile (pre_price_text '2/'), the other")
print("  three are per-item. ON CONFLICT (normalized_name, product_id) DO UPDATE")
print("  only bumps times_seen, so price_is_per_item is set by whichever row is")
print("  inserted first -- silently order-dependent.")

print()
print("=" * 78)
print("CLAIM 4: the provider renders one tile as several product_name values,")
print("including pairs that differ only by an invisible character.")
print("=" * 78)
a = "VILLAGGIO BREAD 550 - 675 G DEMPSTER'S WHOLE GRAIN BREAD 600 G BAGELS 450 - 510 G"
b = a.replace("BREAD 600 G", "BREAD 600 G")
print(f"  equal as text? {a == b}")
print(f"  same normalized key? {normalize_name(a) == normalize_name(b)}")
print("  -> distinct deals.product_name rows, one alias key. normalize_name")
print("     absorbs it correctly; the deals table does not.")
