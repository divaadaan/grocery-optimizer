"""Gate: which deals may reach a user, given their dietary restrictions.

A user with no dietary restrictions is served every current deal.

A user with **any** restriction is served only deals whose linked products are
all **confirmed** and whose curated attributes clear the restrictions
(``app/services/products.py``). Unlinked, proposed and rejected products
withhold the deal: fail closed. Where a deal came from (seed, manual, Flipp)
does not matter; only the reviewed product does.

Product names are not matched against a dietary vocabulary anywhere on the
serving path. Name matching both drops safe food ("Beefsteak Tomatoes",
"Peanut Butter" for a vegan) and misses unsafe food ("marzipan",
"prosciutto"), so the curated attributes are the sole authority.

The gate is applied in SQL, before any ``LIMIT``, so a restricted user is never
handed an empty list while permitted deals sit just past the cap.
"""
from __future__ import annotations

from typing import Iterable

from app.services.products import (
    compliant_product_filter,
    restrictions_are_active,
    restrictions_to_attributes,
)

__all__ = ["deal_filter", "gate_scope", "restrictions_are_active"]


def deal_filter(restrictions: Iterable[str] | None) -> tuple[str, dict]:
    """SQL fragment + params to append to a ``FROM deals d ... WHERE`` query.

    ``("", {})`` for an unrestricted user; otherwise the products gate.
    """
    return compliant_product_filter(restrictions)


def gate_scope(restrictions: Iterable[str] | None) -> str:
    """A stable key for "which deals does this gate let through".

    Two users with the same scope see the same deal set, so it is safe to use
    in a cache key. Unrestricted users share ``"open"``.
    """
    if not restrictions_are_active(restrictions):
        return "open"
    attrs = restrictions_to_attributes(restrictions)
    return "products:" + ",".join(attrs)
