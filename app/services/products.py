"""Canonical products: dietary compliance from curated data.

A ``products`` row carries explicit boolean attributes (``contains_meat``,
``contains_fish``, ``contains_dairy``, ``contains_egg``, ``contains_nuts``,
``contains_animal_byproduct``) that a human confirmed in review. Those
attributes are the **only** authority on whether a deal may be served to a
user with dietary restrictions.

An attribute must be explicitly ``False`` to clear a restriction. ``NULL``
means "nobody has decided" and fails closed, exactly like ``True``. Only
``contains_animal_byproduct`` can be NULL (migration 015 added it to a
catalogue that already had confirmed rows, and inventing a value for them
would be a guess); the other five are NOT NULL. Products whose NULL decides a
verdict are listed by :func:`undecided_products` so a reviewer can settle them.

``contains_animal_byproduct`` is an animal-derived ingredient that is not meat,
fish, dairy or egg: honey, beeswax, carmine/cochineal, shellac/confectioner's
glaze, bone-char-refined sugar, lanolin-derived vitamin D3. Slaughter
by-products that vegetarians also avoid -- gelatin, lard, tallow, animal
rennet -- are ``contains_meat``, NOT this attribute, so that they keep failing
vegetarian and pescatarian. See :data:`ATTRIBUTE_RULES`.

Product names are never matched against a vocabulary. Name matching fails in
both directions on real flyer text -- "Beefsteak Tomatoes" reads as beef while
"marzipan" and "prosciutto" contain no forbidden word at all -- so it can only
add false positives on top of the curated attributes, never safety.

Fail-closed, everywhere
-----------------------
No junction row means "not linked to a confirmed product". It never means
"safe". A user with any dietary restriction is served only deals linked to
**confirmed** products whose attributes clear their restrictions; unlinked,
proposed-but-unconfirmed, and rejected all withhold the deal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
import re
from typing import Any, Iterable

from app.db.database import db

logger = logging.getLogger(__name__)

#: Restriction label -> product attributes that must all be False to comply.
#: The single source of truth for what a restriction label means when serving
#: deals. A label missing here enforces no attribute (see
#: :func:`restrictions_to_attributes`), so a new label belongs here first.
RESTRICTION_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    # Vegan only: honey and the other non-meat animal products are a vegan
    # concern, and vegetarians and pescatarians eat them.
    "vegan": ("contains_meat", "contains_fish", "contains_dairy", "contains_egg",
              "contains_animal_byproduct"),
    "vegetarian": ("contains_meat", "contains_fish"),
    "pescatarian": ("contains_meat",),
    "no_nuts": ("contains_nuts",),
    "nut_free": ("contains_nuts",),
    "dairy_free": ("contains_dairy",),
}

#: Attribute columns, in a stable order, for SQL generation and review display.
ATTRIBUTES: tuple[str, ...] = (
    "contains_meat",
    "contains_fish",
    "contains_dairy",
    "contains_egg",
    "contains_nuts",
    "contains_animal_byproduct",
)

#: The attribute columns as SQL select lists. Built from our own constant,
#: never from input, so interpolating them is safe.
_ATTR_SELECT = ", ".join(ATTRIBUTES)
_P_ATTR_SELECT = ", ".join(f"p.{a}" for a in ATTRIBUTES)

#: Attributes whose column is nullable (NULL = undecided, fails closed).
NULLABLE_ATTRIBUTES: frozenset[str] = frozenset({"contains_animal_byproduct"})

#: What each attribute means, for labellers and reviewers. The proposer's
#: LLM instructions and both review surfaces show these, so the rule a
#: labeller follows and the rule a reviewer checks are the same text.
ATTRIBUTE_RULES: dict[str, str] = {
    "contains_meat": (
        "any mammal or bird flesh, including cured and processed forms, AND "
        "slaughter by-products vegetarians also avoid: gelatin, lard, tallow, "
        "animal rennet"),
    "contains_fish": "any fish OR shellfish",
    "contains_dairy": "milk-derived, including butter, cheese, cream, yogurt, whey",
    "contains_egg": "egg or egg-derived",
    "contains_nuts": "tree nuts or peanuts, including derived pastes and flours",
    "contains_animal_byproduct": (
        "an animal-derived ingredient that is NOT meat, fish, dairy or egg: "
        "honey, beeswax, carmine/cochineal, shellac/confectioner's glaze, "
        "bone-char-refined sugar, lanolin-derived vitamin D3. Gelatin, lard, "
        "tallow and animal rennet are contains_meat, not this"),
}

# Size/pack tokens a flyer appends to a name. Stripped so that
# "LACTANTIA MILK 2 L" and "LACTANTIA MILK, 2L" normalise alike.
_SIZE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*"
    r"(?:g|gr|gram|grams|kg|ml|l|lt|litre|liter|oz|lb|lbs|ea|pk|pack|ct|count|x)\b",
    re.IGNORECASE,
)
# A bare multiplier like "2 x" or "x4".
_MULT_RE = re.compile(r"\b\d+\s*x\b|\bx\s*\d+\b", re.IGNORECASE)
# Price fragments that leak into names ("$4.96", "4/$5").
_PRICE_RE = re.compile(r"\$\s*\d+(?:[.,]\d+)?|\b\d+\s*/\s*\$\s*\d+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(raw: str | None) -> str:
    """Collapse a raw flyer string to a stable lookup key.

    Deliberately conservative: it strips size, pack and price noise and
    punctuation, and nothing else. It does **not** try to strip brands, drop
    adjectives, or stem words -- guessing which token is the brand is how
    "Beefsteak Tomatoes" becomes "beef". Genuinely different strings for one
    product are collapsed by the alias table instead, which is many-to-one and
    human-confirmed.

    Returns ``""`` for input that normalises to nothing, which callers must
    treat as unmappable rather than as a key.
    """
    if not raw:
        return ""
    s = raw.casefold()
    s = _PRICE_RE.sub(" ", s)
    s = _SIZE_RE.sub(" ", s)
    s = _MULT_RE.sub(" ", s)
    s = _NON_WORD_RE.sub(" ", s)
    return " ".join(s.split())


def restrictions_to_attributes(restrictions: Iterable[str] | None) -> tuple[str, ...]:
    """Attributes that must be False for a product to clear ``restrictions``.

    Unknown labels contribute no attribute, but they still count as a
    restriction: :func:`compliant_product_filter` withholds every deal not
    linked to a confirmed product for such a user. Unknown labels ARE logged,
    so a typo'd restriction does not pass unnoticed.
    """
    required: set[str] = set()
    unknown: list[str] = []
    for label in restrictions or []:
        key = (label or "").strip().lower()
        if not key:
            continue
        attrs = RESTRICTION_ATTRIBUTES.get(key)
        if attrs is None:
            unknown.append(key)
            continue
        required.update(attrs)
    if unknown:
        logger.warning(
            "Unrecognised dietary restriction label(s) %s - no product attribute "
            "enforces them. Add them to products.RESTRICTION_ATTRIBUTES.",
            unknown,
        )
    return tuple(sorted(required))


def product_allows(product: dict[str, Any], restrictions: Iterable[str] | None) -> bool:
    """True if this product's curated attributes clear the restrictions.

    Every required attribute must be explicitly ``False``. ``None`` is an
    undecided ``contains_animal_byproduct`` (migration 015) and fails closed.
    A **missing key** means the caller selected a partial row, which is a bug
    worth a warning -- and it fails closed too, because assuming False is how
    an unknown becomes "safe".
    """
    for attr in restrictions_to_attributes(restrictions):
        if attr not in product:
            logger.warning(
                "Product %s has no %s - treating as non-compliant. Select the "
                "full attribute set before calling product_allows().",
                product.get("product_id"),
                attr,
            )
            return False
        value = product[attr]
        if value is None or value:
            return False
    return True


def restrictions_are_active(restrictions: Iterable[str] | None) -> bool:
    """True if the user has at least one non-blank dietary restriction."""
    if not restrictions:
        return False
    return any((r or "").strip() for r in restrictions)


def compliant_product_filter(restrictions: Iterable[str] | None) -> tuple[str, dict]:
    """SQL fragment + params restricting a ``deals d`` query to safe products.

    Returns ``("", {})`` when the user has no restrictions, so the caller can
    skip the join entirely. Otherwise the fragment requires every product
    linked to the deal to be *confirmed* with all the restriction's attributes
    False -- EXISTS clauses, so it composes with the caller's existing WHERE and
    applies before any LIMIT.

    A restriction whose labels map to no attribute (unknown labels) still gets
    the linked-and-confirmed requirement: having a restriction at all is what
    puts a user behind this gate.
    """
    if not restrictions_are_active(restrictions):
        return "", {}
    required = restrictions_to_attributes(restrictions)
    # Attribute names come from our own constant, never from user input.
    for attr in required:
        assert attr in ATTRIBUTES, f"unknown attribute {attr!r}"

    # Reads the deal_products junction, not deals.product_id, because a flyer
    # tile can advertise several products at one price. The rule is EVERY
    # product on the deal must clear the restriction: a tile offering chicken
    # and milk is not vegetarian-safe just because the milk is.
    #
    # Two clauses, and both are load-bearing:
    #   EXISTS     -> the deal has at least one linked product. A deal with no
    #                 junction rows fails, so unlinked is withheld rather than
    #                 vacuously "nothing violates it, let it through".
    #   NOT EXISTS -> no linked product is unconfirmed or violating.
    #
    # "IS NOT FALSE", not a bare column: contains_animal_byproduct is nullable
    # (migration 015), and a bare NULL makes the OR chain NULL rather than
    # true, so NOT EXISTS would read an undecided product as clean -- fail
    # OPEN. IS NOT FALSE is true for both true and NULL.
    violates = " OR ".join(
        ["p.status <> 'confirmed'"] + [f"p.{attr} IS NOT FALSE" for attr in required]
    )
    fragment = f"""
          AND EXISTS (
              SELECT 1 FROM deal_products dp
              WHERE dp.deal_id = d.deal_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM deal_products dp
              JOIN products p ON p.product_id = dp.product_id
              WHERE dp.deal_id = d.deal_id
                AND ({violates})
          )"""
    return fragment, {}


# --------------------------------------------------------------------------
# Linking: deterministic, no LLM, idempotent
# --------------------------------------------------------------------------

def link_deals(database=db) -> dict[str, int]:
    """Attach every deal to the confirmed products its name refers to.

    Writes ``deal_products`` (migration 009), which is authoritative for
    compliance, because one name can refer to several products -- a flyer tile
    advertising three things is one deal row with three products.
    ``deals.product_id`` is still set when a name resolves to exactly one
    product, for the older read paths; a tile leaves it NULL because a single
    column cannot hold three answers.

    Normalisation happens in Python, not SQL, so there is exactly one
    implementation of :func:`normalize_name` and the two cannot drift.
    Idempotent: ``ON CONFLICT DO NOTHING`` plus the ``product_id IS NULL``
    guard means re-running links nothing new and changes no row.
    """
    stats = {
        "examined": 0,
        "linked_deals": 0,
        "linked_snapshots": 0,
        "unmapped": 0,
        "tile_links": 0,
        "blocked_undecided_price": 0,
    }

    with database.get_cursor() as cursor:
        # "Unlinked" means no junction row, not product_id IS NULL: a tile is
        # legitimately left with a NULL product_id, and keying off that would
        # re-link it on every run.
        cursor.execute(
            """
            SELECT DISTINCT d.product_name
            FROM deals d
            WHERE NOT EXISTS (
                SELECT 1 FROM deal_products dp WHERE dp.deal_id = d.deal_id
            )
            """
        )
        names = [r["product_name"] for r in cursor.fetchall()]
        stats["examined"] = len(names)
        if not names:
            return stats

        cursor.execute(
            """
            SELECT normalized_name, product_id, tile_position, price_is_per_item
            FROM product_aliases
            WHERE status = 'confirmed' AND product_id IS NOT NULL
            ORDER BY normalized_name, tile_position
            """
        )
        alias_map: dict[str, list[dict]] = {}
        for row in cursor.fetchall():
            alias_map.setdefault(row["normalized_name"], []).append(dict(row))

        for raw in names:
            targets = alias_map.get(normalize_name(raw))
            if not targets:
                stats["unmapped"] += 1
                continue

            # price_is_per_item is NOT NULL in deal_products, and NULL on the
            # alias means the labeller could not tell whether the tile price
            # applies per item. Refuse rather than default: a wrong "yes" puts
            # a bundle price on a single product's line in a shopping list.
            usable = [t for t in targets if t["price_is_per_item"] is not None]
            if len(usable) < len(targets):
                stats["blocked_undecided_price"] += len(targets) - len(usable)
                logger.warning(
                    "Skipped %d product(s) for %r: price_is_per_item is "
                    "undecided, so no junction row can be written.",
                    len(targets) - len(usable), raw,
                )
            if not usable:
                stats["unmapped"] += 1
                continue

            for target in usable:
                cursor.execute(
                    """
                    INSERT INTO deal_products
                        (deal_id, product_id, tile_position, price_is_per_item)
                    SELECT d.deal_id, %(pid)s, %(pos)s, %(per_item)s
                    FROM deals d
                    WHERE d.product_name = %(raw)s
                    ON CONFLICT (deal_id, product_id) DO NOTHING
                    """,
                    {
                        "pid": target["product_id"],
                        "pos": target["tile_position"],
                        "per_item": target["price_is_per_item"],
                        "raw": raw,
                    },
                )
                stats["linked_deals"] += cursor.rowcount or 0

            if len(usable) == 1:
                # Single-product name: keep the legacy column in step.
                cursor.execute(
                    """
                    UPDATE deals SET product_id = %(pid)s
                    WHERE product_id IS NULL AND product_name = %(raw)s
                    """,
                    {"pid": usable[0]["product_id"], "raw": raw},
                )
                cursor.execute(
                    """
                    UPDATE price_snapshots SET product_id = %(pid)s
                    WHERE product_id IS NULL AND product_name = %(raw)s
                    """,
                    {"pid": usable[0]["product_id"], "raw": raw},
                )
                stats["linked_snapshots"] += cursor.rowcount or 0
            else:
                stats["tile_links"] += 1
                # Snapshots carry one price under one name, so a decomposed
                # tile has no single product to attribute the observation to.
                # Left unlinked deliberately; see the ROADMAP debt entry.

    if stats["linked_deals"]:
        logger.info(
            "Linked %d deal-product row(s) (%d from tiles) and %d snapshot(s); "
            "%d name(s) remain unmapped.",
            stats["linked_deals"], stats["tile_links"],
            stats["linked_snapshots"], stats["unmapped"],
        )
    return stats


def unmapped_names(limit: int = 200, only_current: bool = True, database=db) -> list[dict]:
    """The review queue: deal names with no confirmed product link.

    Reads the ``unmapped_deal_names`` view from migration 008. Ordered by how
    many deals carry the name, so the items being withheld from the most users
    surface first.
    """
    query = "SELECT * FROM unmapped_deal_names"
    if only_current:
        query += " WHERE currently_valid"
    query += " ORDER BY deal_count DESC, product_name LIMIT %(limit)s"
    with database.get_cursor() as cursor:
        cursor.execute(query, {"limit": limit})
        return [dict(r) for r in cursor.fetchall()]


def confirmed_products(database=db) -> list[dict]:
    """Every confirmed product, for the proposer's "match an existing one" set."""
    with database.get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT product_id, canonical_name, category, is_pantry_staple,
                   {_ATTR_SELECT}
            FROM products
            WHERE status = 'confirmed'
            ORDER BY canonical_name
            """
        )
        return [dict(r) for r in cursor.fetchall()]


# --------------------------------------------------------------------------
# Review service: shared by the CLI and the admin web page
# --------------------------------------------------------------------------
#
# These live here rather than in scripts/review_products.py so the terminal
# and the browser cannot drift. A second implementation of "what does
# confirming mean" is exactly the kind of divergence that ends with one
# surface promoting a product the other would have withheld.

#: Restriction labels the app understands, in the order a reviewer thinks
#: about them.
REVIEW_LABELS: tuple[str, ...] = ("vegan", "vegetarian", "pescatarian", "no_nuts")


def clears_labels(product: dict[str, Any]) -> list[str]:
    """Restriction labels this product's attributes clear.

    Shown to the reviewer because it is the actual consequence of the
    checkboxes: "contains dairy" is abstract, "clears vegetarian but not
    vegan" is the decision being made.
    """
    if product.get("product_id") is None:
        return []
    return [label for label in REVIEW_LABELS if product_allows(product, [label])]


def pending_proposals(limit: int = 5000, database=db) -> list[dict]:
    """Proposed aliases with their product, most-seen first."""
    with database.get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT a.alias_id, a.normalized_name, a.raw_example, a.confidence,
                   a.proposal_reason, a.times_seen, a.source,
                   a.tile_position, a.price_is_per_item,
                   p.product_id, p.canonical_name, p.category,
                   p.status AS product_status, p.is_pantry_staple,
                   {_P_ATTR_SELECT},
                   -- Every product this same string maps to. A multi-product
                   -- tile appears in several groups, and without this the
                   -- reviewer sees the whole tile text under one product with
                   -- no sign the rest of it went anywhere.
                   COALESCE(sib.names, ARRAY[]::text[]) AS tile_products
            FROM product_aliases a
            LEFT JOIN products p ON p.product_id = a.product_id
            LEFT JOIN LATERAL (
                SELECT array_agg(p2.canonical_name ORDER BY a2.tile_position)
                       AS names
                FROM product_aliases a2
                JOIN products p2 ON p2.product_id = a2.product_id
                WHERE a2.normalized_name = a.normalized_name
                  AND a2.status <> 'rejected'
            ) sib ON true
            WHERE a.status = 'proposed'
            ORDER BY a.times_seen DESC, a.normalized_name
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )
        return [dict(r) for r in cursor.fetchall()]


def group_proposals(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split pending aliases into product groups and the unplaceable ones.

    Groups are ordered by total ``times_seen``: a group seen often is being
    withheld from users week after week, so it is worth deciding first.
    """
    groups: dict[int, dict] = {}
    orphans: list[dict] = []

    for row in rows:
        if row["product_id"] is None:
            orphans.append(row)
            continue
        group = groups.setdefault(
            row["product_id"],
            {
                "product_id": row["product_id"],
                "canonical_name": row["canonical_name"],
                "category": row["category"],
                "product_status": row["product_status"],
                "is_pantry_staple": row["is_pantry_staple"],
                **{attr: row[attr] for attr in ATTRIBUTES},
                "aliases": [],
            },
        )
        group["aliases"].append(row)

    for group in groups.values():
        group["times_seen"] = sum(a["times_seen"] for a in group["aliases"])
        group["clears"] = clears_labels(group)

    ordered = sorted(
        groups.values(),
        key=lambda g: (-g["times_seen"], g["canonical_name"] or ""),
    )
    orphans.sort(key=lambda r: (-r["times_seen"], r["raw_example"]))
    return ordered, orphans


def set_attributes(
    product_id: int, updates: dict[str, bool], database=db
) -> dict[str, bool]:
    """Overwrite attributes on a not-yet-confirmed product.

    Rejects anything outside ``ATTRIBUTES``: these names are interpolated into
    SQL, so the whitelist is load-bearing, not tidiness.
    """
    unknown = set(updates) - set(ATTRIBUTES)
    if unknown:
        raise ValueError(f"unknown attribute(s): {sorted(unknown)}")
    # None is "undecided", not False: dropped, so the column keeps whatever it
    # holds. bool(None) would silently turn an unknown into "safe".
    clean = {k: bool(v) for k, v in updates.items() if v is not None}
    if not clean:
        return {}
    sets = ", ".join(f"{attr} = %({attr})s" for attr in clean)
    with database.get_cursor() as cursor:
        cursor.execute(
            f"UPDATE products SET {sets} WHERE product_id = %(pid)s",
            {**clean, "pid": product_id},
        )
    return clean


# --------------------------------------------------------------------------
# Undecided attributes on already-confirmed products
# --------------------------------------------------------------------------
#
# Migration 015 added contains_animal_byproduct as a nullable column, so every
# product confirmed before it carries NULL there -- and NULL fails closed, so
# those products are withheld from vegans. They are already confirmed, so the
# proposal queue never shows them again. These two functions are the way back:
# list the products whose NULL is deciding a verdict, and let a reviewer fill
# it in. Filling only ever replaces NULL, so it cannot overrule a decision.

def _null_decides(product: dict[str, Any]) -> list[str]:
    """Review labels this product would clear if its NULL attributes were
    False. Empty means no NULL here changes any verdict -- e.g. chicken with
    an undecided animal-byproduct still fails vegan on contains_meat."""
    decides = []
    for label in REVIEW_LABELS:
        attrs = RESTRICTION_ATTRIBUTES[label]
        values = [product.get(a) for a in attrs]
        if any(v is None for v in values) and not any(values):
            decides.append(label)
    return decides


def undecided_products(database=db) -> list[dict]:
    """Confirmed products with a NULL attribute that decides a verdict.

    Each row carries ``undecided`` (the NULL attributes), ``clears`` (what it
    clears today) and ``would_clear`` (labels a False would open). Products
    already failing every affected label on another attribute are left out:
    no value for the NULL would change what anyone is served.
    """
    nulls = " OR ".join(f"{a} IS NULL" for a in sorted(NULLABLE_ATTRIBUTES))
    with database.get_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT product_id, canonical_name, category, status,
                   is_pantry_staple, {_ATTR_SELECT}
            FROM products
            WHERE status = 'confirmed' AND ({nulls})
            ORDER BY canonical_name
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]
    out = []
    for row in rows:
        would = _null_decides(row)
        if not would:
            continue
        row["undecided"] = [a for a in ATTRIBUTES if row.get(a) is None]
        row["clears"] = clears_labels(row)
        row["would_clear"] = would
        out.append(row)
    return out


def fill_undecided_attributes(
    product_id: int, updates: dict[str, bool], database=db
) -> dict[str, Any]:
    """Set attributes that are currently NULL; never overwrite a decision.

    For confirmed products whose attribute was added after they were reviewed
    (migration 015). ``COALESCE(column, value)`` makes the write a no-op on any
    column that already holds a value, so a stale browser tab or a second
    reviewer cannot flip a decided attribute through this path. Returns the
    row's attribute values after the write.
    """
    unknown = set(updates) - set(ATTRIBUTES)
    if unknown:
        raise ValueError(f"unknown attribute(s): {sorted(unknown)}")
    clean = {k: bool(v) for k, v in updates.items() if v is not None}
    if not clean:
        return {}
    sets = ", ".join(f"{attr} = COALESCE({attr}, %({attr})s)" for attr in clean)
    with database.get_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE products SET {sets}
            WHERE product_id = %(pid)s
            RETURNING {_ATTR_SELECT}
            """,
            {**clean, "pid": product_id},
        )
        row = cursor.fetchone()
    return dict(row) if row else {}


def confirm_group(
    product_id: int, alias_ids: list[int], reviewer: str, database=db
) -> int:
    """Confirm a product and the given aliases together.

    The product is promoted alongside its aliases because the reviewer just
    verified these attributes; requiring a separate product confirmation would
    be busywork that invites rubber-stamping.
    """
    if not alias_ids:
        return 0
    now = datetime.now(timezone.utc)
    with database.get_cursor() as cursor:
        cursor.execute(
            """
            UPDATE products
            SET status = 'confirmed', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE product_id = %(pid)s AND status <> 'confirmed'
            """,
            {"pid": product_id, "who": reviewer, "now": now},
        )
        cursor.execute(
            """
            UPDATE product_aliases
            SET status = 'confirmed', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE alias_id = ANY(%(ids)s) AND status = 'proposed'
            """,
            {"ids": list(alias_ids), "who": reviewer, "now": now},
        )
        return cursor.rowcount or 0


def reject_aliases(alias_ids: list[int], reviewer: str, database=db) -> int:
    """Reject aliases so they stay withheld and stop re-queueing."""
    if not alias_ids:
        return 0
    now = datetime.now(timezone.utc)
    with database.get_cursor() as cursor:
        cursor.execute(
            """
            UPDATE product_aliases
            SET status = 'rejected', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE alias_id = ANY(%(ids)s) AND status = 'proposed'
            """,
            {"ids": list(alias_ids), "who": reviewer, "now": now},
        )
        return cursor.rowcount or 0


def reject_product_if_orphaned(product_id: int, reviewer: str, database=db) -> bool:
    """Reject a product once nothing non-rejected points at it.

    Keeps the live-name unique index free, so a better proposal for the same
    canonical name can be created later.
    """
    now = datetime.now(timezone.utc)
    with database.get_cursor() as cursor:
        cursor.execute(
            """
            UPDATE products
            SET status = 'rejected', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE product_id = %(pid)s
              AND status = 'proposed'
              AND NOT EXISTS (
                  SELECT 1 FROM product_aliases
                  WHERE product_id = %(pid)s AND status <> 'rejected'
              )
            """,
            {"pid": product_id, "who": reviewer, "now": now},
        )
        return bool(cursor.rowcount)


def review_stats(database=db) -> dict[str, int]:
    """Counts for the review UI header."""
    with database.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              (SELECT count(*) FROM product_aliases WHERE status = 'proposed')
                AS proposed,
              (SELECT count(*) FROM product_aliases WHERE status = 'confirmed')
                AS confirmed,
              (SELECT count(*) FROM product_aliases WHERE status = 'rejected')
                AS rejected,
              (SELECT count(*) FROM products WHERE status = 'confirmed')
                AS products_confirmed,
              -- Counted from the junction, which is authoritative. Counting
              -- deals.product_id under-reports: a decomposed tile leaves that
              -- column NULL because one column cannot hold several products,
              -- so every tile looked unlinked (80 vs the real 93).
              (SELECT count(DISTINCT deal_id) FROM deal_products)
                AS deals_linked,
              (SELECT count(*) FROM deals) AS deals_total,
              (SELECT count(*) FROM deals WHERE product_id IS NOT NULL)
                AS deals_single_product
            """
        )
        return dict(cursor.fetchone())


# --------------------------------------------------------------------------
# Price semantics: is sale_price this product's own price?
# --------------------------------------------------------------------------

#: "2 FOR $5", "3/$10", "2/5.00" -- the price covers a quantity, so it is not
#: any single product's price.
#: Three explicit forms rather than one loose "digit, separator, digit". The
#: loose version matched "20/20", which is two numbers either side of a slash
#: and not a quantity -- harmless while this was only applied to sale_story,
#: and a false positive the moment it was also applied to pre_price_text.
#: A slash form therefore has to carry a currency marker or a decimal price.
_MULTIBUY_RE = re.compile(
    r"\b\d+\s*for\b"                 # "2 FOR $5", "4 FOR 10", bare "2 for"
    r"|\b\d+\s*/\s*\$\s*[\d.,]+"     # "3/$10"
    r"|\b\d+\s*/\s*\d+[.,]\d+",      # "2/5.00"
    re.IGNORECASE,
)
#: "buy 2 get 1", "buy one get one", "BOGO" -- same conclusion, different
#: wording. The quantity may be a word rather than a digit, so the gap between
#: "buy" and "get" is matched by length rather than by shape, bounded tightly
#: enough that unrelated text containing both words does not collide.
_BOGO_RE = re.compile(r"\bbogo\b|\bbuy\b[^.!?]{0,12}\bget\b", re.IGNORECASE)

#: A bare quantity prefix printed to the left of the price: "2/", "3 /", and
#: also the worded form "2 for" with the price rendered separately. Anchored at
#: the start so a prefix that merely contains a number ("Save 2 Ways") does not
#: match. The worded form was found live by the 5-term probe: it reached this
#: function as pre_price_text='2 for' and, because only _QTY_PREFIX_RE was
#: consulted for prefixes and it required a slash, returned True -- halving the
#: price in exactly the case the docstring below promises it would not.
#: Two alternatives, not one loose pattern. A trailing `\d*` after the slash
#: would also match "20/20", which is two numbers either side of a slash and
#: not a quantity at all -- reading it as multi-buy would withhold a decidable
#: deal. So: a slash form that is either bare or followed by a CURRENCY amount,
#: or the worded "N for" form.
_QTY_PREFIX_RE = re.compile(
    r"^\s*\d+\s*(?:/\s*(?:\$\s*[\d.,]+)?|for\b.*)$", re.IGNORECASE
)

#: A price floor across a tile's variants rather than a price: "from $4.99",
#: "starting at $4.99". Both renderings observed live; only "from" was handled.
_FLOOR_RE = re.compile(r"\b(?:from|starting\s+at)\b", re.IGNORECASE)

#: A per-item price printed INSIDE the unit field: "or $2.89 each", "2.87 /ea",
#: "or 4.29/ 4.77 ea". 13 rows in the 5-term probe did this. It means the
#: headline sale_price is NOT the single-item price -- the single-item price is
#: the number quoted here -- so the headline must not be read as per-item.
#: Requires a digit, so a bare "ea"/"each" unit is unaffected.
_PRICE_IN_UNIT_RE = re.compile(
    r"\d+[.,]\d{2}.*\b(?:ea|each)\b|\bor\b.*\d+[.,]\d{2}", re.IGNORECASE
)

#: Units that mean the price attaches to one item or to a measure of it, so it
#: IS this product's price. "lb" and "kg" count: $7.99/lb is the price of a
#: pound of whichever product you pick off the tile.
_PER_ITEM_UNITS = frozenset({
    "each", "ea", "ea.", "lb", "lbs", "kg", "100 g", "100g", "unit", "pk", "pack",
    # "pkg" found live by the 5-term probe.
    "pkg",
})


def infer_price_is_per_item(
    unit: str | None,
    sale_story: str | None = None,
    pre_price_text: str | None = None,
) -> bool | None:
    """Does ``deals.sale_price`` apply to one product on its own?

    Returns ``True``, ``False``, or ``None`` for undecided. **None is a real
    answer and must stay one**: ``deal_products.price_is_per_item`` is NOT NULL,
    so an undecided price blocks the link rather than defaulting. A wrong
    ``True`` puts a bundle price on a single product's line in a shopping list,
    which is worse than withholding the deal.

    Precedence is deliberate. A multi-buy story beats a per-item unit, because
    "2 FOR $5" with ``unit='ea.'`` means two items for five dollars, not five
    dollars each -- reading the unit first would halve every such price.
    """
    story = (sale_story or "").strip()
    prefix = (pre_price_text or "").strip()

    # A multi-buy claim can appear in EITHER field, and must be checked in both.
    # Checking only the story is the bug the 5-term probe found: a tile with
    # pre_price_text='2 for' and unit='ea.' returned True, halving the price in
    # precisely the case this function's docstring says it prevents.
    for text in (story, prefix):
        if text and (_MULTIBUY_RE.search(text) or _BOGO_RE.search(text)):
            return False
    # The quantity can also be a bare "2/" to the left of the price -- observed
    # live on "BEATRICE CHOCOLATE MILK OR BUTTER MILK" at $7.00.
    if prefix and _QTY_PREFIX_RE.match(prefix):
        return False
    # "from $4.99" / "starting at $4.99" is a floor across the tile's variants,
    # not a price.
    if prefix and _FLOOR_RE.search(prefix):
        return False
    # A per-item price quoted inside the unit field proves the headline price is
    # not the per-item one.
    if unit and _PRICE_IN_UNIT_RE.search(unit):
        return False

    normalized = (unit or "").strip().lower().lstrip("/")
    if normalized in _PER_ITEM_UNITS:
        return True

    # A savings story with no quantity ("save $10", "SAVE 30%", "Rollback")
    # says nothing about per-item pricing, so it does not decide this either
    # way. Undecided.
    return None


def reinfer_alias_prices(database=db) -> dict[str, int]:
    """Fill NULL ``price_is_per_item`` on proposed aliases from the deal text.

    Runs after ingestion, because the signals live on the deal rows: ``unit``,
    and since migration 011 ``sale_story`` and ``pre_price_text``. A labeller
    working from the name alone often cannot tell, and leaves NULL; the source
    text frequently can.

    Only ever fills NULL, and that is the whole safety property: a value
    already decided -- by the labeller or a reviewer -- is left alone, because
    this is a cheap heuristic and must not overrule a human.

    **Confirmed aliases are included, deliberately.** They were excluded at
    first, on the same "don't overrule a human" reasoning, which conflated two
    different things: a NULL is not a decision, it is the absence of one. The
    cost of that conflation was found live. The 115 aliases confirmed in the
    first review pass predate migration 010, which is what added this column,
    so every one of them carries NULL -- and ``link_deals`` refuses a NULL by
    design. The result was that newly ingested deals under already-confirmed
    names ("Boneless, Skinless Chicken Breasts", "Natrel fine-filtered milk")
    could never link, and no amount of re-running fixed it: the backfill
    skipped them for being confirmed, and review never re-offers a confirmed
    alias. Filling a NULL on a confirmed row overrules nobody.
    """
    stats = {"examined": 0, "filled_true": 0, "filled_false": 0, "still_null": 0}

    with database.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT a.alias_id,
                   max(d.unit)           AS unit,
                   max(d.sale_story)     AS sale_story,
                   max(d.pre_price_text) AS pre_price_text
            FROM product_aliases a
            JOIN deals d ON d.product_name = a.raw_example
            WHERE a.price_is_per_item IS NULL
              -- Proposed AND confirmed. Rejected is excluded because nothing
              -- will ever read it. See the docstring: confirmed rows from
              -- before migration 010 all carry NULL, and skipping them left
              -- newly ingested deals under confirmed names permanently
              -- unlinkable.
              AND a.status IN ('proposed', 'confirmed')
            GROUP BY a.alias_id
            """
        )
        rows = [dict(r) for r in cursor.fetchall()]
        stats["examined"] = len(rows)

        for row in rows:
            verdict = infer_price_is_per_item(
                row["unit"], row["sale_story"], row["pre_price_text"]
            )
            if verdict is None:
                stats["still_null"] += 1
                continue
            cursor.execute(
                """
                UPDATE product_aliases
                SET price_is_per_item = %(v)s
                WHERE alias_id = %(id)s AND price_is_per_item IS NULL
                """,
                {"v": verdict, "id": row["alias_id"]},
            )
            if verdict:
                stats["filled_true"] += 1
            else:
                stats["filled_false"] += 1

    if stats["filled_true"] or stats["filled_false"]:
        logger.info(
            "Re-inferred pricing for %d alias(es) (%d per-item, %d bundle); "
            "%d still undecided.",
            stats["filled_true"] + stats["filled_false"],
            stats["filled_true"], stats["filled_false"], stats["still_null"],
        )
    return stats
