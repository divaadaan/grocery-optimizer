"""Deliberate, audited corrections to a confirmed product's attributes.

A confirmed product's attributes (``products.ATTRIBUTES``) are the allergen
authority: ``deal_gate`` serves deals from them and ``agents/dietary.py``
judges recipes by them. The review flow refuses to change them (PR #24,
``products.AttributeConflict``), because a stale tab or a second reviewer must
not be able to flip ``contains_nuts`` by confirming an alias. Sometimes a
confirmed value is simply wrong, though, and this module is the one way to fix
it:

* only **confirmed** products -- a proposed product is still under review and
  is edited there;
* only true/false -- see :func:`validate_changes` for why NULL is refused;
* every requested attribute must actually change -- a no-op is refused, so an
  audit row always records a real change;
* a reviewer and a free-text reason are required;
* the attribute update and its audit rows (migration 016) commit in **one
  transaction**, so there is never a change without a record or a record
  without a change;
* the caller gets an **impact report**: which deals linked through
  ``deal_products`` become newly withheld or newly servable for which
  restriction labels, and which active users' served deal sets change. A
  ``dry_run`` computes the same report and writes nothing.

The verdicts in the report come from ``products.deal_allows`` /
``products.product_allows`` -- the same functions the serving gate and the
recipe checker are defined by -- evaluated against the product's attributes
before and after. Nothing here looks at a product or deal *name*.

Attributes are iterated from ``products.ATTRIBUTES`` and labels from
``products.RESTRICTION_ATTRIBUTES``, so a new attribute or label flows through
without changes here.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from psycopg.types.json import Jsonb

from app.db.database import db
from app.services import products

logger = logging.getLogger(__name__)

#: ``product_corrections.kind`` for this module. Siblings (rename,
#: recategorise, merge) get their own kind and their own detail table.
KIND = "attribute"

REVIEWER_MAX = 80     # products.reviewed_by / product_corrections.reviewer
REASON_MAX = 2000


class CorrectionInvalid(ValueError):
    """The request itself is malformed (unknown attribute, NULL, blank reason)."""


class ProductNotFound(LookupError):
    """No product with that id."""


class CorrectionRefused(Exception):
    """A well-formed request that the product's current state refuses:
    the product is not confirmed, or a requested value is already stored."""


def validate_changes(changes: dict[str, Any]) -> dict[str, bool]:
    """Whitelist and type-check the requested attribute values.

    Attribute names are interpolated into SQL, so ``products.ATTRIBUTES`` is
    a load-bearing whitelist.

    NULL is refused. It would fail closed, so it is not *unsafe* -- but it
    reopens the attribute to the unaudited fill paths: ``/admin/products/fill``
    and ``set_attributes`` both fill a NULL on a confirmed product without a
    reason or an audit row. Correcting to NULL would therefore let the next
    change to that attribute bypass this module entirely. A correction must
    land a decision.
    """
    if not changes:
        raise CorrectionInvalid("no attribute changes given")
    unknown = sorted(set(changes) - set(products.ATTRIBUTES))
    if unknown:
        raise CorrectionInvalid(
            f"unknown attribute(s): {unknown}; expected some of "
            f"{list(products.ATTRIBUTES)}")
    nulls = sorted(a for a, v in changes.items() if v is None)
    if nulls:
        raise CorrectionInvalid(
            f"cannot correct {nulls} to null: a correction must set true or "
            "false. NULL (undecided) would reopen the attribute to the "
            "unaudited fill paths.")
    not_bool = sorted(a for a, v in changes.items() if not isinstance(v, bool))
    if not_bool:
        raise CorrectionInvalid(f"{not_bool}: values must be true or false")
    # Stable order, for SQL, audit rows and the report.
    return {a: changes[a] for a in products.ATTRIBUTES if a in changes}


def _clean_text(value: str | None, field: str, limit: int) -> str:
    text = (value or "").strip()
    if not text:
        raise CorrectionInvalid(f"{field} is required")
    if len(text) > limit:
        raise CorrectionInvalid(f"{field} is longer than {limit} characters")
    return text


# --------------------------------------------------------------------------
# Impact
# --------------------------------------------------------------------------

def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _group_deals(rows: list[dict]) -> list[dict]:
    """One entry per deal, carrying every product linked to it."""
    deals: dict[int, dict] = {}
    for row in rows:
        deal = deals.setdefault(row["deal_id"], {
            "deal_id": row["deal_id"],
            "product_name": row["product_name"],
            "store_id": row["store_id"],
            "valid_from": _iso(row["valid_from"]),
            "valid_until": _iso(row["valid_until"]),
            "live": bool(row["live"]),
            "linked": [],
        })
        deal["linked"].append({
            "product_id": row["linked_product_id"],
            "status": row["linked_status"],
            **{a: row[a] for a in products.ATTRIBUTES},
        })
    return list(deals.values())


def _restriction_key(restrictions: Any) -> tuple[str, ...]:
    return tuple(sorted({(r or "").strip().lower()
                         for r in (restrictions or []) if (r or "").strip()}))


def compute_impact(
    product: dict[str, Any],
    after: dict[str, Any],
    deal_rows: list[dict],
    user_rows: list[dict],
) -> dict[str, Any]:
    """What a change from ``product``'s attributes to ``after`` would change.

    * ``product_clears`` -- restriction labels the product itself clears before
      and after. This is the staple consequence: a pantry staple cited by
      ``product_id`` in a recipe is judged on exactly this.
    * ``deals`` -- every deal linked to the product through ``deal_products``,
      with the labels for which it becomes newly withheld / newly servable.
      Other products on a multi-product tile take part in the verdict, so a
      tile already withheld by its other product does not flip.
    * ``users`` -- active users with restrictions whose served set of *live*
      deals (``valid_until >= today``) changes, by their actual restriction
      list, which may combine labels.

    Pure: no database access, so it is the same computation for a dry run and
    a real correction.
    """
    pid = product["product_id"]
    labels = sorted(products.RESTRICTION_ATTRIBUTES)

    def with_after(linked: list[dict]) -> list[dict]:
        return [{**p, **after} if p["product_id"] == pid else p for p in linked]

    def clears(attrs: dict) -> list[str]:
        return [lab for lab in labels if products.product_allows(attrs, [lab])]

    before_clears, after_clears = clears(product), clears({**product, **after})

    deals = []
    for deal in _group_deals(deal_rows):
        linked_after = with_after(deal["linked"])
        withheld, servable = [], []
        for label in labels:
            was = products.deal_allows(deal["linked"], [label])
            now = products.deal_allows(linked_after, [label])
            if was and not now:
                withheld.append(label)
            elif now and not was:
                servable.append(label)
        deals.append({
            **{k: deal[k] for k in ("deal_id", "product_name", "store_id",
                                    "valid_from", "valid_until", "live")},
            "other_products": [p["product_id"] for p in deal["linked"]
                               if p["product_id"] != pid],
            "newly_withheld_for": withheld,
            "newly_servable_for": servable,
            "_linked": deal["linked"],
            "_linked_after": linked_after,
        })

    # Users: only a restriction that reads a changed attribute can flip a
    # verdict, and users sharing a restriction set share every verdict, so
    # evaluate each distinct set once.
    changed = set(after)
    live = [d for d in deals if d["live"]]
    verdicts: dict[tuple[str, ...], tuple[list[int], list[int]]] = {}
    users = []
    for user in user_rows:
        key = _restriction_key(user.get("dietary_restrictions"))
        if not key:
            continue
        if key not in verdicts:
            withheld_ids: list[int] = []
            servable_ids: list[int] = []
            if changed & set(products.restrictions_to_attributes(key)):
                for d in live:
                    was = products.deal_allows(d["_linked"], key)
                    now = products.deal_allows(d["_linked_after"], key)
                    if was and not now:
                        withheld_ids.append(d["deal_id"])
                    elif now and not was:
                        servable_ids.append(d["deal_id"])
            verdicts[key] = (withheld_ids, servable_ids)
        withheld_ids, servable_ids = verdicts[key]
        if withheld_ids or servable_ids:
            users.append({
                "user_id": user["user_id"],
                "dietary_restrictions": list(user.get("dietary_restrictions") or []),
                "newly_withheld_deal_ids": withheld_ids,
                "newly_servable_deal_ids": servable_ids,
            })

    for d in deals:
        d.pop("_linked")
        d.pop("_linked_after")

    changed_deals = [d for d in deals
                     if d["newly_withheld_for"] or d["newly_servable_for"]]
    return {
        "product_clears": {
            "before": before_clears,
            "after": after_clears,
            "newly_cleared": [lab for lab in after_clears if lab not in before_clears],
            "newly_failing": [lab for lab in before_clears if lab not in after_clears],
        },
        "deals": deals,
        "users": users,
        "summary": {
            "deals_linked": len(deals),
            "deals_live": len(live),
            "deals_changed": len(changed_deals),
            "live_deals_changed": sum(1 for d in changed_deals if d["live"]),
            "users_affected": len(users),
        },
    }


# --------------------------------------------------------------------------
# Reads and the write
# --------------------------------------------------------------------------

# Select lists are built per call from products.ATTRIBUTES (our own constant,
# never input), so an attribute added there is read here with no change.

def _load_product(cursor, product_id: int, lock: bool) -> dict | None:
    attrs = ", ".join(products.ATTRIBUTES)
    cursor.execute(
        f"""
        SELECT product_id, canonical_name, category, status, is_pantry_staple,
               {attrs}
        FROM products
        WHERE product_id = %(pid)s
        {"FOR UPDATE" if lock else ""}
        """,
        {"pid": product_id},
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _load_deal_rows(cursor, product_id: int) -> list[dict]:
    """Every deal linked to the product, one row per (deal, linked product)."""
    attrs = ", ".join(f"p.{a}" for a in products.ATTRIBUTES)
    cursor.execute(
        f"""
        SELECT d.deal_id, d.product_name, d.store_id,
               d.valid_from, d.valid_until,
               (d.valid_until >= CURRENT_DATE) AS live,
               p.product_id AS linked_product_id, p.status AS linked_status,
               {attrs}
        FROM deal_products target
        JOIN deals d ON d.deal_id = target.deal_id
        JOIN deal_products dp ON dp.deal_id = d.deal_id
        JOIN products p ON p.product_id = dp.product_id
        WHERE target.product_id = %(pid)s
        ORDER BY d.deal_id, dp.tile_position, p.product_id
        """,
        {"pid": product_id},
    )
    return [dict(r) for r in cursor.fetchall()]


def _load_restricted_users(cursor) -> list[dict]:
    cursor.execute(
        """
        SELECT user_id, dietary_restrictions
        FROM users
        WHERE is_active IS TRUE
          AND jsonb_typeof(dietary_restrictions) = 'array'
          AND jsonb_array_length(dietary_restrictions) > 0
        ORDER BY user_id
        """
    )
    return [dict(r) for r in cursor.fetchall()]


def correct_attributes(
    product_id: int,
    changes: dict[str, Any],
    reviewer: str | None,
    reason: str | None,
    dry_run: bool = False,
    database=db,
) -> dict[str, Any]:
    """Correct a confirmed product's attributes, or preview doing so.

    Raises :class:`CorrectionInvalid` (bad request), :class:`ProductNotFound`
    or :class:`CorrectionRefused` (unconfirmed product, no-op value) before
    anything is written. Otherwise returns the report; with ``dry_run`` nothing
    is written and no row lock is taken.
    """
    wanted = validate_changes(changes)
    who = _clean_text(reviewer, "reviewer", REVIEWER_MAX)
    why = _clean_text(reason, "reason", REASON_MAX)

    # One cursor == one connection == one transaction: the pool commits when
    # the block exits cleanly and rolls back if anything inside raises, so the
    # audit rows and the UPDATE land together or not at all.
    with database.get_cursor() as cursor:
        product = _load_product(cursor, product_id, lock=not dry_run)
        if product is None:
            raise ProductNotFound(f"no product {product_id}")
        if product["status"] != "confirmed":
            raise CorrectionRefused(
                f"product {product_id} ({product['canonical_name']!r}) is "
                f"{product['status']}, not confirmed: edit its attributes in "
                "review (/admin/review) instead.")
        no_ops = [a for a, v in wanted.items() if product[a] is v]
        if no_ops:
            raise CorrectionRefused(
                f"product {product_id} ({product['canonical_name']!r}) already "
                f"stores {', '.join(f'{a}={product[a]}' for a in no_ops)}; "
                "a correction must change every attribute it names.")

        changes_out = [{"attribute": a, "old_value": product[a], "new_value": v}
                       for a, v in wanted.items()]
        impact = compute_impact(
            product, wanted,
            _load_deal_rows(cursor, product_id),
            _load_restricted_users(cursor),
        )
        report: dict[str, Any] = {
            "dry_run": dry_run,
            "correction_id": None,
            "corrected_at": None,
            "product": {
                "product_id": product_id,
                "canonical_name": product["canonical_name"],
                "category": product["category"],
                "status": product["status"],
                "is_pantry_staple": product["is_pantry_staple"],
                "before": {a: product[a] for a in products.ATTRIBUTES},
                "after": {a: wanted.get(a, product[a]) for a in products.ATTRIBUTES},
            },
            "changes": changes_out,
            "reviewer": who,
            "reason": why,
            "impact": impact,
        }
        if dry_run:
            return report

        cursor.execute(
            """
            INSERT INTO product_corrections
                (product_id, kind, reviewer, reason, impact)
            VALUES (%(pid)s, %(kind)s, %(who)s, %(why)s, %(impact)s)
            RETURNING correction_id, corrected_at
            """,
            {"pid": product_id, "kind": KIND, "who": who, "why": why,
             "impact": Jsonb(impact)},
        )
        header = cursor.fetchone()
        for change in changes_out:
            cursor.execute(
                """
                INSERT INTO product_attribute_corrections
                    (correction_id, attribute, old_value, new_value)
                VALUES (%(cid)s, %(attribute)s, %(old_value)s, %(new_value)s)
                """,
                {"cid": header["correction_id"], **change},
            )
        sets = ", ".join(f"{a} = %({a})s" for a in wanted)
        cursor.execute(
            f"UPDATE products SET {sets} "
            "WHERE product_id = %(pid)s AND status = 'confirmed'",
            {**wanted, "pid": product_id},
        )
        if cursor.rowcount != 1:
            # Cannot happen under the row lock; raising rolls back the audit
            # rows rather than recording a change that did not land.
            raise RuntimeError(
                f"correction of product {product_id} updated "
                f"{cursor.rowcount} rows; rolled back")

    report["correction_id"] = header["correction_id"]
    report["corrected_at"] = _iso(header["corrected_at"])
    logger.warning(
        "Attribute correction %s on product %s (%r) by %r: %s -- %s",
        report["correction_id"], product_id, product["canonical_name"], who,
        ", ".join(f"{c['attribute']} {c['old_value']}->{c['new_value']}"
                  for c in changes_out),
        why,
    )
    return report
