#!/usr/bin/env python
"""Prove the products gate end-to-end against the live schema, then roll back.

Answers the one question the catalog exists to answer: with a confirmed
product in place, does a user with dietary restrictions actually receive
ingested deals again? And does the gate still withhold the ones it should?

Everything here runs in a single transaction that is **rolled back**. No
product is confirmed, no alias is created, no deal is linked. Confirming a
dietary label is a human decision (scripts/review_products.py); this script
only demonstrates that the machinery works once a human makes it.
"""
from __future__ import annotations

import os
import sys

import psycopg
from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
load_dotenv(os.path.join(REPO, ".env"))

from app.services.products import compliant_product_filter, normalize_name  # noqa: E402

BASE_QUERY = """
SELECT count(*) FROM deals d
JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = %(postal_code)s
  AND d.valid_until >= CURRENT_DATE
  AND d.valid_from <= CURRENT_DATE
"""
POSTAL = "M5V3A8"


def count_for(cur, restrictions) -> int:
    fragment, extra = compliant_product_filter(restrictions)
    cur.execute(BASE_QUERY + fragment, {"postal_code": POSTAL, **extra})
    return cur.fetchone()[0]


def main() -> int:
    url = os.environ["DATABASE_URL"]
    conn = psycopg.connect(url, autocommit=False)
    try:
        with conn.cursor() as cur:
            print("=== BEFORE: empty catalog (fail-closed) ===")
            print(f"  unrestricted      {count_for(cur, [])}")
            print(f"  vegetarian        {count_for(cur, ['vegetarian'])}   <- expect 0")
            print(f"  vegan             {count_for(cur, ['vegan'])}   <- expect 0")

            # Pick a real ingested dairy item. Dairy clears vegetarian but not
            # vegan, so one product demonstrates both directions of the gate.
            cur.execute(
                """
                SELECT d.product_name, count(*) AS n
                FROM deals d JOIN stores s ON d.store_id = s.store_id
                WHERE s.postal_code = %(p)s
                  AND d.valid_until >= CURRENT_DATE
                  -- Must match the gate's window exactly. Flyers are ingested
                  -- before they start, so filtering only on valid_until picks
                  -- a deal that is valid tomorrow and counts zero today.
                  AND d.valid_from <= CURRENT_DATE
                  AND d.source = 'flipp'
                  AND d.product_name ILIKE '%%milk%%'
                GROUP BY d.product_name ORDER BY n DESC LIMIT 1
                """,
                {"p": POSTAL},
            )
            row = cur.fetchone()
            if not row:
                print("\nNo ingested milk deal to demonstrate with; aborting.")
                conn.rollback()
                return 1
            raw_name, deal_count = row
            normalized = normalize_name(raw_name)
            print(f"\n=== confirming one product for {raw_name!r} "
                  f"({deal_count} deal(s)) ===")
            print(f"  normalized -> {normalized!r}")

            cur.execute(
                """
                INSERT INTO products (
                    canonical_name, category, contains_meat, contains_fish,
                    contains_dairy, contains_egg, contains_nuts,
                    status, proposed_by, reviewed_by, reviewed_at
                ) VALUES (
                    'milk (verification)', 'dairy', false, false,
                    true, false, false,
                    'confirmed', 'verification', 'verification', now()
                ) RETURNING product_id
                """
            )
            product_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO product_aliases (
                    normalized_name, raw_example, product_id, status,
                    source, confidence, reviewed_by, reviewed_at
                ) VALUES (
                    %(norm)s, %(raw)s, %(pid)s, 'confirmed',
                    'verification', 1.0, 'verification', now()
                )
                """,
                {"norm": normalized, "raw": raw_name, "pid": product_id},
            )
            # The link step, inlined so it shares this transaction.
            cur.execute(
                "UPDATE deals SET product_id = %(pid)s WHERE product_name = %(raw)s",
                {"pid": product_id, "raw": raw_name},
            )
            linked = cur.rowcount
            print(f"  linked {linked} deal(s)")

            print("\n=== AFTER: one confirmed dairy product ===")
            unrestricted = count_for(cur, [])
            vegetarian = count_for(cur, ["vegetarian"])
            vegan = count_for(cur, ["vegan"])
            nut_free = count_for(cur, ["no_nuts"])
            print(f"  unrestricted      {unrestricted}")
            print(f"  vegetarian        {vegetarian}   <- expect {linked} (dairy is OK)")
            print(f"  vegan             {vegan}   <- expect 0 (dairy is not)")
            print(f"  no_nuts           {nut_free}   <- expect {linked} (no nuts)")

            ok = (
                vegetarian == linked
                and vegan == 0
                and nut_free == linked
                and unrestricted >= linked
            )
            print("\n" + ("PASS - the gate opens for a cleared restriction and "
                          "stays shut for one it does not clear"
                          if ok else "FAIL - see the numbers above"))
            return 0 if ok else 1
    finally:
        conn.rollback()
        conn.close()
        print("\n(transaction rolled back - nothing was confirmed or linked)")


if __name__ == "__main__":
    raise SystemExit(main())
