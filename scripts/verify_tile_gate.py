#!/usr/bin/env python
"""Prove the junction gate handles multi-product tiles, then roll back.

The question this answers: if one deal advertises several products, does the
gate require ALL of them to clear the restriction? A tile offering chicken
and milk must not be served to a vegetarian just because the milk is fine.

Runs in a single transaction that is **rolled back**. Nothing is decomposed,
nothing is confirmed.
"""
from __future__ import annotations

import os
import sys

import psycopg
from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
load_dotenv(os.path.join(REPO, ".env"))

from app.services.products import compliant_product_filter  # noqa: E402

BASE = """
SELECT count(*) FROM deals d
JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = %(postal_code)s
  AND d.valid_until >= CURRENT_DATE
  AND d.valid_from <= CURRENT_DATE
"""
POSTAL = "M5V3A8"


def count(cur, restrictions) -> int:
    fragment, extra = compliant_product_filter(restrictions)
    cur.execute(BASE + fragment, {"postal_code": POSTAL, **extra})
    return cur.fetchone()[0]


def main() -> int:
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=False)
    try:
        with conn.cursor() as cur:
            print("=== baseline (no tile decomposed) ===")
            base = {
                label or "none": count(cur, [label] if label else [])
                for label in (None, "vegetarian", "vegan", "no_nuts")
            }
            for name, n in base.items():
                print(f"  {name:<12} {n:>3}")

            # Find a confirmed meat product and a confirmed dairy product.
            cur.execute(
                """
                SELECT product_id, canonical_name FROM products
                WHERE status = 'confirmed' AND contains_meat LIMIT 1
                """
            )
            meat = cur.fetchone()
            cur.execute(
                """
                SELECT product_id, canonical_name FROM products
                WHERE status = 'confirmed' AND contains_dairy
                  AND NOT contains_meat LIMIT 1
                """
            )
            dairy = cur.fetchone()
            if not meat or not dairy:
                print("\nneed one confirmed meat and one confirmed dairy product")
                return 1

            # Take a currently-valid deal that is already linked to the dairy
            # product only, and add the meat product to it -- simulating a tile
            # decomposed into both.
            cur.execute(
                """
                SELECT d.deal_id FROM deals d
                JOIN stores s ON d.store_id = s.store_id
                JOIN deal_products dp ON dp.deal_id = d.deal_id
                WHERE s.postal_code = %(p)s
                  AND d.valid_until >= CURRENT_DATE
                  AND d.valid_from <= CURRENT_DATE
                  AND dp.product_id = %(dairy)s
                LIMIT 1
                """,
                {"p": POSTAL, "dairy": dairy[0]},
            )
            row = cur.fetchone()
            if not row:
                print("\nno currently-valid deal linked to the dairy product")
                return 1
            deal_id = row[0]

            print(f"\n=== simulating a tile: deal {deal_id} advertises both ===")
            print(f"  {dairy[1]} (dairy)  +  {meat[1]} (meat)")
            cur.execute(
                """
                INSERT INTO deal_products
                    (deal_id, product_id, tile_position, price_is_per_item)
                VALUES (%(d)s, %(p)s, 1, true)
                """,
                {"d": deal_id, "p": meat[0]},
            )

            after = {
                label or "none": count(cur, [label] if label else [])
                for label in (None, "vegetarian", "vegan", "no_nuts")
            }
            print("\n=== after ===")
            for name, n in after.items():
                delta = n - base[name]
                print(f"  {name:<12} {n:>3}  ({delta:+d})")

            # The dairy-only deal cleared vegetarian before. Now that the same
            # deal also advertises meat, it must not.
            ok = (
                after["vegetarian"] == base["vegetarian"] - 1
                and after["none"] == base["none"]
                and after["vegan"] == base["vegan"]
            )
            print()
            if ok:
                print("PASS - adding a meat product to the tile withdrew the deal "
                      "from vegetarians, and left unrestricted users untouched")
            else:
                print("FAIL - a tile containing meat is still reaching vegetarians")
            return 0 if ok else 1
    finally:
        conn.rollback()
        conn.close()
        print("\n(transaction rolled back - no tile was decomposed)")


if __name__ == "__main__":
    raise SystemExit(main())
