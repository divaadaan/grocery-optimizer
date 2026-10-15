#!/usr/bin/env python
"""Snapshot DB counts so the probe can be shown to be additive."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import db

QUERIES = {
    "deals_total": "SELECT count(*) AS n FROM deals",
    "deals_current": "SELECT count(*) AS n FROM deals WHERE valid_until >= CURRENT_DATE",
    "products_total": "SELECT count(*) AS n FROM products",
    "products_confirmed": "SELECT count(*) AS n FROM products WHERE status='confirmed'",
    "products_proposed": "SELECT count(*) AS n FROM products WHERE status='proposed'",
    "aliases_total": "SELECT count(*) AS n FROM product_aliases",
    "aliases_confirmed": "SELECT count(*) AS n FROM product_aliases WHERE status='confirmed'",
    "aliases_proposed": "SELECT count(*) AS n FROM product_aliases WHERE status='proposed'",
    "aliases_rejected": "SELECT count(*) AS n FROM product_aliases WHERE status='rejected'",
    "deal_products": "SELECT count(*) AS n FROM deal_products",
    "distinct_names": "SELECT count(DISTINCT product_name) AS n FROM deals",
    "stores": "SELECT count(*) AS n FROM stores",
}


def main() -> int:
    with db.get_cursor() as cursor:
        for label, sql in QUERIES.items():
            cursor.execute(sql)
            print(f"{label:<22} {cursor.fetchone()['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
