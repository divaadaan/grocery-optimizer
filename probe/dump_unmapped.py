#!/usr/bin/env python
"""Dump the unmapped queue with everything needed to label and to analyse it."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import db
from app.services.products import normalize_name

SQL = """
SELECT d.product_name,
       count(*)                                        AS deal_count,
       count(DISTINCT d.store_id)                       AS store_count,
       min(d.sale_price)                                AS min_price,
       max(d.sale_price)                                AS max_price,
       array_agg(DISTINCT d.unit)                       AS units,
       array_agg(DISTINCT d.sale_story)                 AS sale_stories,
       array_agg(DISTINCT d.pre_price_text)             AS pre_price_texts,
       array_agg(DISTINCT coalesce(d.category, '<null>')) AS categories,
       min(d.created_at)                                AS first_created,
       -- This probe's ingest is the 2026-10-15 00:33 cluster. The 00:07
       -- cluster (5 deals, Meat & Poultry + Dairy & Eggs) is the OTHER
       -- agent's run and must not be counted as ours.
       bool_or(d.created_at >= TIMESTAMPTZ '2026-10-15 00:30+00') AS from_this_run,
       bool_and(d.created_at >= TIMESTAMPTZ '2026-10-15 00:30+00') AS only_this_run,
       bool_or(EXISTS (SELECT 1 FROM deal_products dp
                       WHERE dp.deal_id = d.deal_id))   AS has_junction_row
FROM deals d
WHERE d.product_id IS NULL
GROUP BY d.product_name
ORDER BY count(*) DESC, d.product_name
"""


def main() -> int:
    with db.get_cursor() as cursor:
        cursor.execute(SQL)
        rows = [dict(r) for r in cursor.fetchall()]

    out = []
    for r in rows:
        out.append({
            "product_name": r["product_name"],
            "normalized": normalize_name(r["product_name"]),
            "deal_count": r["deal_count"],
            "store_count": r["store_count"],
            "min_price": float(r["min_price"]),
            "max_price": float(r["max_price"]),
            "units": [u for u in (r["units"] or []) if u is not None],
            "sale_stories": [u for u in (r["sale_stories"] or []) if u is not None],
            "pre_price_texts": [u for u in (r["pre_price_texts"] or []) if u is not None],
            "categories": r["categories"],
            "new_this_run": bool(r["only_this_run"]),
            "seen_before": not bool(r["only_this_run"]),
            "has_junction_row": bool(r["has_junction_row"]),
        })

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unmapped_dump.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)

    print(f"rows: {len(out)}")
    print(f"new this run: {sum(1 for r in out if r['new_this_run'])}")
    print(f"seen before:  {sum(1 for r in out if r['seen_before'])}")
    print(f"WITH junction rows but still in unmapped view: "
          f"{sum(1 for r in out if r['has_junction_row'])}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
