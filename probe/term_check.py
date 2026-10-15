#!/usr/bin/env python
"""Check created_at clustering, and category attribution for the 5 probe terms."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import db
from app.services.flipp_staples import category_for_term

for t in ["peanut butter", "tomatoes", "bread", "canned tuna", "tofu"]:
    print(f"  category_for_term({t!r}) = {category_for_term(t)!r}")

print()
with db.get_cursor() as cursor:
    cursor.execute(
        """
        SELECT date_trunc('minute', created_at) AS minute,
               count(*) AS deals, count(DISTINCT product_name) AS names
        FROM deals GROUP BY 1 ORDER BY 1
        """
    )
    print("created_at clusters:")
    for r in cursor.fetchall():
        print(f"  {r['minute']}  deals={r['deals']:<4} names={r['names']}")

    cursor.execute("SELECT now() AS n, CURRENT_DATE AS d")
    r = cursor.fetchone()
    print(f"\ndb now={r['n']}  CURRENT_DATE={r['d']}")

    cursor.execute(
        """
        SELECT coalesce(category, '<null>') AS cat,
               count(*) AS deals, count(DISTINCT product_name) AS names
        FROM deals
        WHERE created_at >= now() - INTERVAL '45 minutes'
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    print("\ncategories among deals created in the last 45 min:")
    for r in cursor.fetchall():
        print(f"  {r['cat']:<16} deals={r['deals']:<4} names={r['names']}")

    cursor.execute(
        """
        SELECT product_name, category, created_at
        FROM deals
        WHERE category = 'Meat & Poultry' AND created_at >= now() - INTERVAL '45 minutes'
        ORDER BY product_name
        """
    )
    print("\nMeat & Poultry rows created in this run:")
    for r in cursor.fetchall():
        print(f"  {r['created_at']}  {r['product_name']}")
