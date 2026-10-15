"""Read-only product lookups the recipe pipeline cites against.

Two reads, both run once per generation by the graph's ``load_recipe_catalog``
node, so the SousChefs and the Nutritionist work from one consistent snapshot:

* :func:`fetch_pantry_staples` -- confirmed products flagged
  ``assumed_on_hand`` (migration 014). The SousChef may cite these by
  ``product_id`` without a deal.
* :func:`fetch_deal_products` -- for each deal, the products linked to it
  through ``deal_products`` (migration 009), with ``status`` and the attribute
  columns. The junction rather than ``deals.product_id``, because a
  multi-product flyer tile leaves that column NULL and the deal gate already
  reads the junction; the two must agree on what a deal *is*.

Nothing here writes.
"""
from __future__ import annotations

from typing import Iterable

from app.db.database import db
from app.services.products import ATTRIBUTES

_ATTR_COLUMNS = ", ".join(f"p.{a}" for a in ATTRIBUTES)


def fetch_pantry_staples(database=db) -> list[dict]:
    """Confirmed always-on-hand staples, with their attributes, by name."""
    with database.get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT p.product_id, p.canonical_name, p.category, p.status,
                   {_ATTR_COLUMNS}
            FROM products p
            WHERE p.status = 'confirmed' AND p.assumed_on_hand
            ORDER BY p.canonical_name
            """
        )
        return [dict(r) for r in cursor.fetchall()]


def fetch_deal_products(deal_ids: Iterable, database=db) -> dict[str, list[dict]]:
    """``str(deal_id)`` -> every product linked to that deal.

    Unconfirmed products are returned too, with their ``status``, so the
    verdict can fail closed on them explicitly rather than by omission. A
    deal with no junction rows is simply absent from the result.
    """
    ids = sorted({int(i) for i in deal_ids if i is not None and str(i).strip().isdigit()})
    if not ids:
        return {}
    with database.get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT dp.deal_id, p.product_id, p.canonical_name, p.category,
                   p.status, {_ATTR_COLUMNS}
            FROM deal_products dp
            JOIN products p ON p.product_id = dp.product_id
            WHERE dp.deal_id = ANY(%(ids)s)
            ORDER BY dp.deal_id, dp.tile_position, p.product_id
            """,
            {"ids": ids},
        )
        out: dict[str, list[dict]] = {}
        for row in cursor.fetchall():
            row = dict(row)
            out.setdefault(str(row.pop("deal_id")), []).append(row)
        return out
