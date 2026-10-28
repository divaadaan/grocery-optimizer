"""Audited renames and merges of curated products (migration 018).

Two siblings of :mod:`app.services.product_corrections`, sharing its audit log
(``product_corrections``, one header row per event), its exceptions, its
single-``get_cursor`` transaction pattern and its impact machinery:

* :func:`rename_product` -- change a product's ``canonical_name`` and/or
  ``category`` (kind ``identity``). **Never an attribute.** A new identity can
  imply different attributes (P84 "cheese tofu" is really fish paste and
  probably needs ``contains_fish``), but that is a separate, separately
  audited act through ``/attribute-corrections``; the response points the
  reviewer there and does not chain it.
* :func:`merge_products` -- fold a duplicate (the *absorbed* product) into a
  *survivor* (kind ``merge``): repoint its aliases, deal links, legacy
  ``deals.product_id`` and price snapshots, and retire it as
  ``status='merged'`` with ``merged_into`` set. Never a DELETE.

What a name and a category drive
--------------------------------
Neither decides dietary compliance -- only the attributes do. ``category`` on
``products`` drives nothing but display and the proposer's prompt
(``deals.category`` is a separate column). ``canonical_name`` drives: the live
name uniqueness index (``uq_products_canonical_name_live``, case-insensitive,
proposed + confirmed only), the proposer's "is this an existing product"
lookup by name, and the label shown for staples and in recipe feedback. Alias
matching keys on ``product_aliases.normalized_name``, not on canonical names,
so a rename moves no deal.

The no-loosening rule (merge)
-----------------------------
A merge must not make any deal newly servable to any restriction label. It is
enforced up front by :func:`loosening_attributes`, and the impact report of
every accepted merge is then checked for zero newly-servable entries as a
guard (:class:`LooseningGuardTripped`), so a gap in the rule fails loudly
instead of serving anyone. See :func:`loosening_attributes` for the rule.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import psycopg.errors
from psycopg.types.json import Jsonb

from app.db.database import db
from app.services import products
from app.services.product_corrections import (
    REASON_MAX,
    REVIEWER_MAX,
    CorrectionInvalid,
    CorrectionRefused,
    ProductNotFound,
    _clean_text,
    _iso,
    _load_deal_rows,
    _load_restricted_users,
    verdict_changes,
)

logger = logging.getLogger(__name__)

IDENTITY_KIND = "identity"
MERGE_KIND = "merge"

NAME_MAX = 120        # products.canonical_name VARCHAR(120)
CATEGORY_MAX = 50     # products.category VARCHAR(50)

#: Statuses that hold a live name (the partial unique index, migration 018).
LIVE_STATUSES = ("proposed", "confirmed")


class LooseningGuardTripped(RuntimeError):
    """A merge that passed :func:`loosening_attributes` would still make a
    deal newly servable. That is a bug in the rule, not a user error, so it
    is not a :class:`CorrectionRefused`; nothing has been written."""


class NameCollision(CorrectionRefused):
    """The requested canonical_name is already held by another live product."""

    def __init__(self, message: str, other: dict):
        super().__init__(message)
        self.other = other


def attribute_correction_url(product_id: int) -> str:
    return f"/admin/products/{product_id}/attribute-corrections"


def _load(cursor, product_id: int, lock: bool) -> dict | None:
    attrs = ", ".join(products.ATTRIBUTES)
    cursor.execute(
        f"""
        SELECT product_id, canonical_name, category, status, merged_into,
               is_pantry_staple, assumed_on_hand, {attrs}
        FROM products
        WHERE product_id = %(pid)s
        {"FOR UPDATE" if lock else ""}
        """,
        {"pid": product_id},
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _attrs(product: dict) -> dict[str, Any]:
    return {a: product.get(a) for a in products.ATTRIBUTES}


def _clears(product: dict) -> list[str]:
    return [lab for lab in sorted(products.RESTRICTION_ATTRIBUTES)
            if products.product_allows(product, [lab])]


def _refuse_retired(product: dict, role: str = "product") -> None:
    pid, name = product["product_id"], product["canonical_name"]
    if product["status"] == "merged":
        raise CorrectionRefused(
            f"{role} {pid} ({name!r}) was already merged into product "
            f"{product['merged_into']}; act on product "
            f"{product['merged_into']} instead.")
    if product["status"] == "rejected":
        raise CorrectionRefused(
            f"{role} {pid} ({name!r}) is rejected, and a rejected product "
            "serves nobody.")


# --------------------------------------------------------------------------
# Rename / recategorise
# --------------------------------------------------------------------------

def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise CorrectionInvalid("canonical_name must be a string")
    text = " ".join(value.split())
    if not text:
        raise CorrectionInvalid("canonical_name is blank")
    if len(text) > NAME_MAX:
        raise CorrectionInvalid(f"canonical_name is longer than {NAME_MAX} characters")
    return text


def _clean_category(value: Any) -> str:
    if not isinstance(value, str):
        raise CorrectionInvalid("category must be a string")
    # Lowercase, like every category the proposer writes.
    text = " ".join(value.split()).lower()
    if not text:
        raise CorrectionInvalid("category is blank")
    if len(text) > CATEGORY_MAX:
        raise CorrectionInvalid(f"category is longer than {CATEGORY_MAX} characters")
    return text


def validate_identity(canonical_name: Any = None, category: Any = None) -> dict[str, str]:
    wanted: dict[str, str] = {}
    if canonical_name is not None:
        wanted["canonical_name"] = _clean_name(canonical_name)
    if category is not None:
        wanted["category"] = _clean_category(category)
    if not wanted:
        raise CorrectionInvalid("give a canonical_name and/or a category to change")
    return wanted


def _name_holder(cursor, product_id: int, name: str) -> dict | None:
    cursor.execute(
        """
        SELECT product_id, canonical_name, status
        FROM products
        WHERE lower(canonical_name) = lower(%(name)s)
          AND status IN ('proposed', 'confirmed')
          AND product_id <> %(pid)s
        """,
        {"name": name, "pid": product_id},
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _collision(product: dict, other: dict) -> NameCollision:
    pid, oid = product["product_id"], other["product_id"]
    if other["status"] == "confirmed" and product["status"] == "confirmed":
        hint = (f"If they are the same product, merge one into the other: "
                f"POST /admin/products/{pid}/merge with {{\"into\": {oid}}} "
                f"(preview with ?dry_run=true).")
    else:
        hint = (f"Product {oid} is {other['status']}: confirm or reject it in "
                "review (/admin/review) first, or pick another name.")
    return NameCollision(
        f"canonical_name {other['canonical_name']!r} is already used by "
        f"product {oid} ({other['status']}); names are unique "
        f"case-insensitively among live products. {hint}",
        other,
    )


def rename_product(
    product_id: int,
    canonical_name: Any = None,
    category: Any = None,
    reviewer: str | None = None,
    reason: str | None = None,
    dry_run: bool = False,
    database=db,
) -> dict[str, Any]:
    """Change a product's canonical_name and/or category, with an audit row.

    Proposed and confirmed products may be renamed; merged and rejected ones
    may not. Every named field must actually change (a no-op is refused, so
    every audit row is a real change). A name held by another live product is
    a :class:`NameCollision` (409) that suggests a merge. No attribute is read
    for writing or written: the UPDATE names only the identity columns.
    """
    wanted = validate_identity(canonical_name, category)
    who = _clean_text(reviewer, "reviewer", REVIEWER_MAX)
    why = _clean_text(reason, "reason", REASON_MAX)

    try:
        with database.get_cursor() as cursor:
            product = _load(cursor, product_id, lock=not dry_run)
            if product is None:
                raise ProductNotFound(f"no product {product_id}")
            _refuse_retired(product)
            no_ops = [f for f, v in wanted.items() if product[f] == v]
            if no_ops:
                raise CorrectionRefused(
                    f"product {product_id} already has "
                    f"{', '.join(f'{f}={product[f]!r}' for f in no_ops)}; a "
                    "rename must change every field it names.")
            if "canonical_name" in wanted:
                other = _name_holder(cursor, product_id, wanted["canonical_name"])
                if other is not None:
                    raise _collision(product, other)

            changes = [{"field": f, "old_value": product[f], "new_value": v}
                       for f, v in wanted.items()]
            report: dict[str, Any] = {
                "dry_run": dry_run,
                "correction_id": None,
                "corrected_at": None,
                "product": {
                    "product_id": product_id,
                    "status": product["status"],
                    "before": {f: product[f] for f in ("canonical_name", "category")},
                    "after": {f: wanted.get(f, product[f])
                              for f in ("canonical_name", "category")},
                    # Unchanged, by construction. Shown so the reviewer can
                    # check them against the new identity.
                    "attributes": _attrs(product),
                    "clears": _clears(product),
                },
                "changes": changes,
                "reviewer": who,
                "reason": why,
                "attributes_changed": False,
                "attribute_review": {
                    "endpoint": attribute_correction_url(product_id),
                    "note": (
                        "A rename never changes attributes. If the new identity "
                        "implies different ones (e.g. a fish product needs "
                        "contains_fish=true), correct them separately there, "
                        "with its own reviewer and reason; preview with "
                        "?dry_run=true."
                        if product["status"] == "confirmed" else
                        "A rename never changes attributes. This product is "
                        "still proposed: check its attributes when you review "
                        "it at /admin/review."),
                },
            }
            if dry_run:
                return report

            cursor.execute(
                """
                INSERT INTO product_corrections
                    (product_id, kind, reviewer, reason, impact)
                VALUES (%(pid)s, %(kind)s, %(who)s, %(why)s, NULL)
                RETURNING correction_id, corrected_at
                """,
                {"pid": product_id, "kind": IDENTITY_KIND, "who": who, "why": why},
            )
            header = cursor.fetchone()
            for change in changes:
                cursor.execute(
                    """
                    INSERT INTO product_identity_corrections
                        (correction_id, field, old_value, new_value)
                    VALUES (%(cid)s, %(field)s, %(old_value)s, %(new_value)s)
                    """,
                    {"cid": header["correction_id"], **change},
                )
            # Field names come from validate_identity's fixed keys, never input.
            sets = ", ".join(f"{f} = %({f})s" for f in wanted)
            cursor.execute(
                f"UPDATE products SET {sets} "
                "WHERE product_id = %(pid)s AND status = %(status)s",
                {**wanted, "pid": product_id, "status": product["status"]},
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"rename of product {product_id} updated {cursor.rowcount} "
                    "rows; rolled back")
    except psycopg.errors.UniqueViolation as exc:
        # A live product took the name between our check and the write.
        raise CorrectionRefused(
            f"canonical_name {wanted.get('canonical_name')!r} was taken by "
            "another product while renaming; nothing was written. Retry to "
            "see which.") from exc

    report["correction_id"] = header["correction_id"]
    report["corrected_at"] = _iso(header["corrected_at"])
    logger.warning(
        "Identity correction %s on product %s by %r: %s -- %s",
        report["correction_id"], product_id, who,
        ", ".join(f"{c['field']} {c['old_value']!r}->{c['new_value']!r}"
                  for c in changes),
        why,
    )
    return report


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------

def _rank(value: Any) -> int:
    """How restrictive a stored attribute value is, for the merge rule.

    True (2) > NULL (1) > False (0). True and NULL both fail closed today, but
    NULL is weaker than True: the unaudited fill paths
    (``fill_undecided_attributes``, ``set_attributes`` on a confirmed row)
    may turn a NULL into False without a reason or an audit row. A survivor
    holding NULL where the absorbed product held True would let the next fill
    unlock the absorbed product's deals with no record."""
    if value is None:
        return 1
    return 2 if value else 0


def loosening_attributes(absorbed: dict, survivor: dict) -> list[str]:
    """Attributes on which the survivor is LESS restrictive than the absorbed.

    The rule: for every attribute in ``products.ATTRIBUTES``, the survivor's
    value must rank at least as high as the absorbed product's, with
    True > NULL > False (see :func:`_rank`). Equivalently, refuse when

    * absorbed True  and survivor NULL or False, or
    * absorbed NULL  and survivor False.

    Why it is sufficient: every deal linked to the absorbed product ends up
    linked to the survivor instead (or already was). A deal is servable for a
    label only if every linked product is confirmed and has False on each of
    the label's attributes. If the survivor has False on an attribute, the
    rule forces the absorbed product to have had False there too, so
    whatever the survivor clears the absorbed product already cleared, and
    swapping one for the other (or dropping the absorbed one from a deal that
    links both) cannot make a deal newly servable. Both are confirmed, so the
    status clause cannot flip either. Unknown labels read no attribute and
    only the status clause, so they cannot flip.

    Empty list == allowed. Iterates ``products.ATTRIBUTES``, so a new
    attribute is covered without changes here.
    """
    return [a for a in products.ATTRIBUTES
            if _rank(survivor.get(a)) < _rank(absorbed.get(a))]


def _plan_aliases(absorbed_aliases: list[dict], survivor_aliases: list[dict],
                  absorbed: dict, survivor: dict) -> tuple[list[dict], list[dict]]:
    """Split the absorbed product's aliases into repoint / retire.

    ``product_aliases`` is unique on (normalized_name, product_id) (migration
    010), so an alias whose string the survivor already maps cannot be
    repointed. That absorbed copy is retired instead -- set ``rejected`` and
    left pointing at the merged product, where it is inert -- and the
    survivor's copy carries the string. One case is refused rather than
    resolved: the absorbed copy is confirmed but the survivor's is not.
    Retiring the confirmed copy would stop that string linking at all, and
    promoting the survivor's would be a review decision taken on the
    reviewer's behalf.
    """
    on_survivor = {a["normalized_name"]: a for a in survivor_aliases}
    repoint, retire, blocked = [], [], []
    for alias in absorbed_aliases:
        twin = on_survivor.get(alias["normalized_name"])
        if twin is None:
            repoint.append(alias)
        elif alias["status"] == "confirmed" and twin["status"] != "confirmed":
            blocked.append((alias, twin))
        else:
            retire.append({**alias, "survivor_alias_id": twin["alias_id"]})
    if blocked:
        detail = "; ".join(
            f"alias {a['alias_id']} {a['normalized_name']!r} is confirmed on "
            f"product {absorbed['product_id']} but alias {t['alias_id']} for "
            f"the same string is {t['status']} on product "
            f"{survivor['product_id']}"
            for a, t in blocked)
        raise CorrectionRefused(
            f"cannot merge: {detail}. Decide the survivor's alias in review "
            "(/admin/review) first; nothing was written.")
    return repoint, retire


def _merge_transform(absorbed_id: int, survivor: dict):
    survivor_linked = {"product_id": survivor["product_id"],
                       "status": survivor["status"], **_attrs(survivor)}

    def with_after(linked: list[dict]) -> list[dict]:
        out, seen = [], set()
        for p in linked:
            p = survivor_linked if p["product_id"] == absorbed_id else p
            if p["product_id"] in seen:     # the deal already linked the survivor
                continue
            seen.add(p["product_id"])
            out.append(p)
        return out

    return with_after


def compute_merge_impact(absorbed: dict, survivor: dict, deal_rows: list[dict],
                         user_rows: list[dict]) -> dict[str, Any]:
    """What folding ``absorbed`` into ``survivor`` changes for serving.

    Same shape as :func:`product_corrections.compute_impact`: per-deal
    ``newly_withheld_for`` / ``newly_servable_for`` labels, affected users,
    and a summary. ``product_clears`` compares the labels each product clears
    (the staple consequence: a recipe citing the absorbed product by
    ``product_id`` fails closed after the merge). Pure.
    """
    absorbed_clears, survivor_clears = _clears(absorbed), _clears(survivor)
    return {
        "product_clears": {
            "absorbed": absorbed_clears,
            "survivor": survivor_clears,
            "newly_cleared": [lab for lab in survivor_clears if lab not in absorbed_clears],
            "newly_failing": [lab for lab in absorbed_clears if lab not in survivor_clears],
        },
        **verdict_changes(absorbed["product_id"], deal_rows,
                          _merge_transform(absorbed["product_id"], survivor),
                          user_rows),
    }


def assert_no_loosening(impact: dict[str, Any]) -> None:
    """The guard: an accepted merge makes nothing newly servable."""
    deals = [d["deal_id"] for d in impact["deals"] if d["newly_servable_for"]]
    users = [u["user_id"] for u in impact["users"] if u["newly_servable_deal_ids"]]
    if deals or users or impact["product_clears"]["newly_cleared"]:
        raise LooseningGuardTripped(
            "merge refused by the no-loosening guard: it would make deals "
            f"{deals} newly servable (users {users}; labels "
            f"{impact['product_clears']['newly_cleared']}) even though the "
            "attribute rule passed. Nothing was written. This is a bug.")


def _staple_warnings(absorbed: dict, survivor: dict) -> list[str]:
    out = []
    for flag in ("assumed_on_hand", "is_pantry_staple"):
        if absorbed.get(flag) and not survivor.get(flag):
            out.append(
                f"product {absorbed['product_id']} has {flag}=true and product "
                f"{survivor['product_id']} does not; the merge does not copy "
                "it, so this flag is lost unless set on the survivor.")
    return out


def _product_summary(p: dict) -> dict[str, Any]:
    return {
        "product_id": p["product_id"],
        "canonical_name": p["canonical_name"],
        "category": p["category"],
        "status": p["status"],
        "is_pantry_staple": p.get("is_pantry_staple"),
        "assumed_on_hand": p.get("assumed_on_hand"),
        "attributes": _attrs(p),
    }


def merge_products(
    absorbed_id: int,
    survivor_id: int,
    reviewer: str | None,
    reason: str | None,
    dry_run: bool = False,
    database=db,
) -> dict[str, Any]:
    """Fold ``absorbed_id`` into ``survivor_id``, or preview doing so.

    Refuses, before writing anything: a self-merge (:class:`CorrectionInvalid`);
    a missing product (:class:`ProductNotFound`); and
    (:class:`CorrectionRefused`) either product not confirmed, the absorbed
    product already merged, a survivor that was merged, an absorbed product
    that other products were merged into (a chain), any attribute on which
    the survivor is less restrictive (:func:`loosening_attributes`), and an
    alias conflict review must settle (:func:`_plan_aliases`).

    Both rows are locked ``FOR UPDATE`` in ascending product_id order, so two
    merges touching the same pair cannot deadlock. Everything -- audit rows,
    repoints and the retirement -- is one transaction.
    """
    if isinstance(absorbed_id, bool) or isinstance(survivor_id, bool):
        raise CorrectionInvalid("product ids must be integers")
    if absorbed_id == survivor_id:
        raise CorrectionInvalid(f"cannot merge product {absorbed_id} into itself")
    who = _clean_text(reviewer, "reviewer", REVIEWER_MAX)
    why = _clean_text(reason, "reason", REASON_MAX)

    try:
        with database.get_cursor() as cursor:
            loaded = {pid: _load(cursor, pid, lock=not dry_run)
                      for pid in sorted((absorbed_id, survivor_id))}
            absorbed, survivor = loaded[absorbed_id], loaded[survivor_id]
            for pid, row in ((absorbed_id, absorbed), (survivor_id, survivor)):
                if row is None:
                    raise ProductNotFound(f"no product {pid}")
            _refuse_retired(absorbed, "absorbed product")
            _refuse_retired(survivor, "survivor")
            for role, row in (("absorbed product", absorbed), ("survivor", survivor)):
                if row["status"] != "confirmed":
                    raise CorrectionRefused(
                        f"{role} {row['product_id']} ({row['canonical_name']!r}) "
                        f"is {row['status']}: only confirmed products are "
                        "merged. Settle it in review (/admin/review) first.")

            cursor.execute(
                "SELECT product_id FROM products WHERE merged_into = %(pid)s "
                "ORDER BY product_id",
                {"pid": absorbed_id},
            )
            absorbed_into_it = [r["product_id"] for r in cursor.fetchall()]
            if absorbed_into_it:
                raise CorrectionRefused(
                    f"product {absorbed_id} has products {absorbed_into_it} "
                    "merged into it; merging it too would chain merges. Merge "
                    f"product {survivor_id} into it instead, or leave it.")

            looser = loosening_attributes(absorbed, survivor)
            if looser:
                detail = ", ".join(
                    f"{a}: absorbed {absorbed.get(a)}, survivor {survivor.get(a)}"
                    for a in looser)
                raise CorrectionRefused(
                    f"merge refused: it would loosen {detail}. The survivor must "
                    "be at least as restrictive on every attribute (true > "
                    "undecided > false). If the survivor is wrong, correct it "
                    f"first via POST {attribute_correction_url(survivor_id)}; if "
                    "the absorbed product is wrong, correct it via POST "
                    f"{attribute_correction_url(absorbed_id)}. Then merge. "
                    "Nothing was written.")

            cursor.execute(
                """
                SELECT alias_id, normalized_name, status, tile_position,
                       price_is_per_item
                FROM product_aliases
                WHERE product_id = %(pid)s
                ORDER BY alias_id
                """,
                {"pid": absorbed_id},
            )
            absorbed_aliases = [dict(r) for r in cursor.fetchall()]
            survivor_aliases: list[dict] = []
            if absorbed_aliases:
                cursor.execute(
                    """
                    SELECT alias_id, normalized_name, status
                    FROM product_aliases
                    WHERE product_id = %(pid)s AND normalized_name = ANY(%(names)s)
                    """,
                    {"pid": survivor_id,
                     "names": [a["normalized_name"] for a in absorbed_aliases]},
                )
                survivor_aliases = [dict(r) for r in cursor.fetchall()]
            alias_repoint, alias_retire = _plan_aliases(
                absorbed_aliases, survivor_aliases, absorbed, survivor)

            cursor.execute(
                """
                SELECT dp.deal_id, dp.tile_position, dp.price_is_per_item,
                       EXISTS (SELECT 1 FROM deal_products s
                               WHERE s.deal_id = dp.deal_id
                                 AND s.product_id = %(sid)s) AS survivor_linked
                FROM deal_products dp
                WHERE dp.product_id = %(aid)s
                ORDER BY dp.deal_id
                """,
                {"aid": absorbed_id, "sid": survivor_id},
            )
            links = [dict(r) for r in cursor.fetchall()]
            link_repoint = [lk for lk in links if not lk["survivor_linked"]]
            link_drop = [lk for lk in links if lk["survivor_linked"]]

            cursor.execute(
                "SELECT deal_id FROM deals WHERE product_id = %(aid)s ORDER BY deal_id",
                {"aid": absorbed_id},
            )
            legacy = [r["deal_id"] for r in cursor.fetchall()]
            cursor.execute(
                "SELECT count(*) AS n FROM price_snapshots WHERE product_id = %(aid)s",
                {"aid": absorbed_id},
            )
            snapshots = cursor.fetchone()["n"]

            impact = compute_merge_impact(
                absorbed, survivor,
                _load_deal_rows(cursor, absorbed_id),
                _load_restricted_users(cursor),
            )
            assert_no_loosening(impact)

            moves = {
                "aliases_repointed": [a["alias_id"] for a in alias_repoint],
                "aliases_retired": [a["alias_id"] for a in alias_retire],
                "deal_links_repointed": [lk["deal_id"] for lk in link_repoint],
                "deal_links_dropped": [lk["deal_id"] for lk in link_drop],
                "legacy_deals_repointed": legacy,
                "snapshots_repointed": snapshots,
            }
            report: dict[str, Any] = {
                "dry_run": dry_run,
                "correction_id": None,
                "corrected_at": None,
                "absorbed": _product_summary(absorbed),
                "survivor": _product_summary(survivor),
                "moves": moves,
                "reviewer": who,
                "reason": why,
                "warnings": _staple_warnings(absorbed, survivor),
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
                {"pid": absorbed_id, "kind": MERGE_KIND, "who": who, "why": why,
                 "impact": Jsonb({"moves": moves, **impact})},
            )
            header = cursor.fetchone()
            cid = header["correction_id"]

            cursor.execute(
                "UPDATE price_snapshots SET product_id = %(sid)s "
                "WHERE product_id = %(aid)s",
                {"sid": survivor_id, "aid": absorbed_id},
            )
            moves["snapshots_repointed"] = cursor.rowcount or 0
            cursor.execute(
                """
                INSERT INTO product_merge_corrections
                    (correction_id, survivor_product_id, snapshots_repointed)
                VALUES (%(cid)s, %(sid)s, %(n)s)
                """,
                {"cid": cid, "sid": survivor_id, "n": moves["snapshots_repointed"]},
            )

            audit: list[tuple[str, int, dict | None]] = []
            _write_moves(cursor, absorbed_id, survivor_id, who,
                         alias_repoint, alias_retire, link_repoint, link_drop,
                         moves, audit)
            for action, row_id, detail in audit:
                cursor.execute(
                    """
                    INSERT INTO product_merge_moves
                        (correction_id, action, row_id, detail)
                    VALUES (%(cid)s, %(action)s, %(row_id)s, %(detail)s)
                    """,
                    {"cid": cid, "action": action, "row_id": row_id,
                     "detail": Jsonb(detail) if detail is not None else None},
                )

            cursor.execute(
                """
                UPDATE products
                SET status = 'merged', merged_into = %(sid)s
                WHERE product_id = %(aid)s AND status = 'confirmed'
                """,
                {"sid": survivor_id, "aid": absorbed_id},
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"retiring product {absorbed_id} updated {cursor.rowcount} "
                    "rows; rolled back")
    except psycopg.errors.UniqueViolation as exc:
        # An alias or deal link for the survivor appeared between the plan
        # and the write (a concurrent proposer or link run).
        raise CorrectionRefused(
            f"merge of {absorbed_id} into {survivor_id} collided with a "
            "concurrent alias or deal-link change; nothing was written. Retry "
            "the dry run.") from exc

    report["correction_id"] = cid
    report["corrected_at"] = _iso(header["corrected_at"])
    logger.warning(
        "Merge %s: product %s (%r) into %s (%r) by %r: %s -- %s",
        cid, absorbed_id, absorbed["canonical_name"], survivor_id,
        survivor["canonical_name"], who,
        ", ".join(f"{k}={v if isinstance(v, int) else len(v)}"
                  for k, v in moves.items()),
        why,
    )
    return report


def _write_moves(cursor, absorbed_id, survivor_id, who, alias_repoint,
                 alias_retire, link_repoint, link_drop, moves, audit) -> None:
    """Apply the planned repoints. Each statement is pinned to the absorbed
    product and to the planned ids, and its RETURNING set must equal the plan:
    a row that moved under us aborts the whole merge."""

    def expect(what: str, planned: list[int], got: list[int]) -> None:
        if sorted(planned) != sorted(got):
            raise RuntimeError(
                f"merge {absorbed_id}->{survivor_id}: {what} planned {sorted(planned)} "
                f"but touched {sorted(got)}; rolled back")

    if alias_repoint:
        ids = [a["alias_id"] for a in alias_repoint]
        cursor.execute(
            """
            UPDATE product_aliases SET product_id = %(sid)s
            WHERE alias_id = ANY(%(ids)s) AND product_id = %(aid)s
            RETURNING alias_id
            """,
            {"sid": survivor_id, "aid": absorbed_id, "ids": ids},
        )
        expect("alias repoint", ids, [r["alias_id"] for r in cursor.fetchall()])
        audit += [("alias_repointed", a["alias_id"], {"status": a["status"]})
                  for a in alias_repoint]

    live_retire = [a for a in alias_retire if a["status"] != "rejected"]
    if live_retire:
        ids = [a["alias_id"] for a in live_retire]
        cursor.execute(
            """
            UPDATE product_aliases
            SET status = 'rejected', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE alias_id = ANY(%(ids)s) AND product_id = %(aid)s
              AND status <> 'rejected'
            RETURNING alias_id
            """,
            {"who": who, "now": datetime.now(timezone.utc), "ids": ids,
             "aid": absorbed_id},
        )
        expect("alias retire", ids, [r["alias_id"] for r in cursor.fetchall()])
    audit += [("alias_retired", a["alias_id"],
               {"old_status": a["status"],
                "survivor_alias_id": a["survivor_alias_id"]})
              for a in alias_retire]

    if link_repoint:
        ids = [lk["deal_id"] for lk in link_repoint]
        cursor.execute(
            """
            UPDATE deal_products SET product_id = %(sid)s
            WHERE product_id = %(aid)s AND deal_id = ANY(%(ids)s)
            RETURNING deal_id
            """,
            {"sid": survivor_id, "aid": absorbed_id, "ids": ids},
        )
        expect("deal link repoint", ids, [r["deal_id"] for r in cursor.fetchall()])
        audit += [("deal_link_repointed", lk["deal_id"], None) for lk in link_repoint]

    if link_drop:
        ids = [lk["deal_id"] for lk in link_drop]
        # deal_products is a junction, not an audit table; the deal keeps its
        # survivor row, and the dropped row is preserved in the move detail.
        cursor.execute(
            """
            DELETE FROM deal_products
            WHERE product_id = %(aid)s AND deal_id = ANY(%(ids)s)
            RETURNING deal_id
            """,
            {"aid": absorbed_id, "ids": ids},
        )
        expect("deal link drop", ids, [r["deal_id"] for r in cursor.fetchall()])
        audit += [("deal_link_dropped", lk["deal_id"],
                   {"tile_position": lk["tile_position"],
                    "price_is_per_item": lk["price_is_per_item"]})
                  for lk in link_drop]

    cursor.execute(
        """
        UPDATE deals SET product_id = %(sid)s
        WHERE product_id = %(aid)s
        RETURNING deal_id
        """,
        {"sid": survivor_id, "aid": absorbed_id},
    )
    legacy = sorted(r["deal_id"] for r in cursor.fetchall())
    moves["legacy_deals_repointed"] = legacy
    audit += [("deal_legacy_repointed", d, None) for d in legacy]
