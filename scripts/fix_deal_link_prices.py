#!/usr/bin/env python
"""Recompute deal_products.price_is_per_item per deal; dry-run by default.

Why: link_deals used to copy the flag from the name-keyed alias, so a
"2/$5" deal linked through an alias marked per-item got a per-item price
(wrong by the multi-buy quantity on the shopping list). Links are now decided
per deal by ``resolve_link_price_is_per_item``; this corrects rows written
before that change, using the SAME Python function (no SQL regex copy).

Only ever flips True -> False (a multi-buy in the deal's own text). It never
sets a row to True and never touches a row whose deal text is undecided.
Idempotent: a second run finds nothing to change.

    python scripts/fix_deal_link_prices.py            # dry-run: list changes
    python scripts/fix_deal_link_prices.py --apply    # write them
    python scripts/fix_deal_link_prices.py --deal 76 --deal 98 --apply
                                                      # only these deals

Rollback: set price_is_per_item = TRUE for the deal_id/product_id pairs the
--apply run printed.
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
load_dotenv(os.path.join(REPO, ".env"))

from app.services.products import resolve_link_price_is_per_item  # noqa: E402

SELECT_SQL = """
SELECT dp.deal_id, dp.product_id, dp.price_is_per_item,
       d.product_name, d.sale_price, d.unit, d.sale_story, d.pre_price_text
FROM deal_products dp
JOIN deals d ON d.deal_id = dp.deal_id
ORDER BY dp.deal_id, dp.product_id
"""


def compute_changes(rows) -> list[dict]:
    """Rows whose stored flag differs from the per-deal answer.

    ``rows`` are dicts as returned by SELECT_SQL. The per-deal rule starts
    from the stored (alias-derived) value, so it can only demote True ->
    False, never promote.
    """
    changes = []
    for r in rows:
        new = resolve_link_price_is_per_item(
            r["price_is_per_item"], r["unit"], r["sale_story"],
            r["pre_price_text"])
        if new is not None and new != r["price_is_per_item"]:
            changes.append({**r, "new": new})
    return changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a read-only dry-run)")
    ap.add_argument("--deal", type=int, action="append", default=[],
                    help="limit to this deal_id (repeatable); default all")
    args = ap.parse_args(argv)

    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=False,
                           row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            cur.execute(SELECT_SQL)
            rows = cur.fetchall()
            changes = compute_changes(rows)
            if args.deal:
                wanted = set(args.deal)
                missing = wanted - {c["deal_id"] for c in changes}
                if missing:
                    print(f"no change computed for deal(s) {sorted(missing)}")
                changes = [c for c in changes if c["deal_id"] in wanted]
            print(f"{len(rows)} deal_products rows examined, "
                  f"{len(changes)} would change")
            for c in changes:
                print(f"  deal {c['deal_id']} product {c['product_id']}: "
                      f"{c['price_is_per_item']} -> {c['new']}  "
                      f"{c['product_name']!r} ${c['sale_price']} "
                      f"unit={c['unit']!r} story={c['sale_story']!r} "
                      f"pre={c['pre_price_text']!r}")
            if not args.apply:
                print("dry-run: nothing written (use --apply)")
                conn.rollback()
                return 0
            for c in changes:
                cur.execute(
                    "UPDATE deal_products SET price_is_per_item = %s "
                    "WHERE deal_id = %s AND product_id = %s "
                    "AND price_is_per_item IS DISTINCT FROM %s",
                    (c["new"], c["deal_id"], c["product_id"], c["new"]))
            conn.commit()
            print(f"applied {len(changes)} change(s)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
