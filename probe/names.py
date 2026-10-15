#!/usr/bin/env python
"""The authoritative indexed list of names this probe must label.

Order is the dump's order, which is deterministic (deal_count DESC, name).
Everything downstream keys off THIS index, never off a retyped string -- two
of the names differ only in invisible characters.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def selected() -> list[dict]:
    with open(os.path.join(HERE, "unmapped_dump.json"), encoding="utf-8") as fh:
        rows = json.load(fh)
    with open("/tmp/probe.json", encoding="utf-8") as fh:
        exported = set(json.load(fh)["unmapped"])
    return [r for r in rows if r["new_this_run"] and r["product_name"] in exported]


if __name__ == "__main__":
    sel = selected()
    print(f"{len(sel)} names")
    for i, r in enumerate(sel, 1):
        name = r["product_name"]
        # Show any non-ASCII so an invisible difference is visible.
        odd = sorted({c for c in name if ord(c) > 126})
        flag = f"   <<non-ascii: {[hex(ord(c)) for c in odd]}>>" if odd else ""
        print(f"{i:>3} |{name}|{flag}")
