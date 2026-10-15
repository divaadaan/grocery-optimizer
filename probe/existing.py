#!/usr/bin/env python
"""Print the confirmed catalogue from the export bundle."""
import json

ATTRS = ("contains_meat", "contains_fish", "contains_dairy",
         "contains_egg", "contains_nuts")

bundle = json.load(open("/tmp/probe.json", encoding="utf-8"))
print(f"{len(bundle['existing_products'])} confirmed product(s)\n")
for p in bundle["existing_products"]:
    flags = [k.replace("contains_", "") for k in ATTRS if p.get(k)]
    name = p["canonical_name"]
    cat = str(p.get("category"))
    print(f"  {name:<28} {cat:<16} {','.join(flags) or '-'}")
print(f"\nunmapped in export: {len(bundle['unmapped'])}")
