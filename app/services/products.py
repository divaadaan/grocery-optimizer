"""Canonical products: compliance from curated data, not from string matching.

Why this module exists
----------------------
``app/agents/dietary.py`` decides compliance by casefold-substring matching a
fixed vocabulary against whatever string the flyer printed. Its own docstring
states the precondition: *"No current seed/deal item collides."* Real ingested
names break it in both directions, and the dangerous direction is the quiet
one -- ``prosciutto``, ``capicollo``, ``kielbasa``, ``pancetta``, ``gelatin``,
``lard`` and ``marzipan`` contain no term in that vocabulary at all. No amount
of tightening to token boundaries fixes those: they are a *vocabulary* problem,
not a *matching* problem.

So compliance moves off the string and onto the product. A ``products`` row
carries explicit boolean attributes a human confirmed; this module reads them.

The authority question, stated plainly
--------------------------------------
Curated attributes are **authoritative**. The substring oracle is kept, but as
an *alarm*, not a veto -- and the alarm is wired into review
(``scripts/review_products.py``), not into the serving path. Two reasons:

1. The oracle cannot catch the cases that motivated this work. ``marzipan``
   contains no nut term, so an oracle veto adds no protection where protection
   is actually needed.
2. A veto would reintroduce the false positives. If a curator names a product
   "beefsteak tomato", a conjunction of oracle-and-attributes would withhold a
   vegan vegetable from a vegetarian -- the exact failure the attributes exist
   to end.

Where the oracle earns its keep is at confirmation time: showing a reviewer
"the substring oracle reads `beef` in this name -- is ``contains_meat=false``
right?" catches human mislabelling at the moment the human is already looking.

Fail-closed, everywhere
-----------------------
``deals.product_id IS NULL`` means "not linked to a confirmed product". It
never means "safe". A user with any dietary restriction is served only deals
linked to a **confirmed** product whose attributes clear their restrictions;
unlinked, proposed-but-unconfirmed, and rejected all withhold the deal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
import re
from typing import Any, Iterable

from app.agents import dietary
from app.db.database import db

logger = logging.getLogger(__name__)

#: Restriction label -> product attributes that must all be False to comply.
#: This mirrors ``dietary.forbidden_terms_for`` exactly: whatever that function
#: forbids by substring, this forbids by attribute. Keeping the two in step is
#: the point -- if a restriction label is added to one, it belongs in both.
RESTRICTION_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "vegan": ("contains_meat", "contains_fish", "contains_dairy", "contains_egg"),
    "vegetarian": ("contains_meat", "contains_fish"),
    "pescatarian": ("contains_meat",),
    "no_nuts": ("contains_nuts",),
    "nut_free": ("contains_nuts",),
}

#: Attribute columns, in a stable order, for SQL generation and review display.
ATTRIBUTES: tuple[str, ...] = (
    "contains_meat",
    "contains_fish",
    "contains_dairy",
    "contains_egg",
    "contains_nuts",
)

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

    Unknown labels contribute nothing -- they are not silently treated as
    "allows everything", because a label this module does not understand also
    does not appear in :data:`RESTRICTION_ATTRIBUTES` for the oracle, and the
    caller's gate withholds unlinked deals regardless. Unknown labels ARE
    logged, so a typo'd restriction does not pass unnoticed.
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
            "enforces them. Add them to products.RESTRICTION_ATTRIBUTES and to "
            "dietary.forbidden_terms_for together.",
            unknown,
        )
    return tuple(sorted(required))


def product_allows(product: dict[str, Any], restrictions: Iterable[str] | None) -> bool:
    """True if this product's curated attributes clear the restrictions.

    A product whose row is missing an attribute is **not** compliant. The
    column is NOT NULL in the schema, so a missing key means the caller
    selected a partial row -- failing closed beats assuming False.
    """
    for attr in restrictions_to_attributes(restrictions):
        value = product.get(attr)
        if value is None:
            logger.warning(
                "Product %s has no %s - treating as non-compliant. Select the "
                "full attribute set before calling product_allows().",
                product.get("product_id"),
                attr,
            )
            return False
        if value:
            return False
    return True


def oracle_disagreement(product: dict[str, Any]) -> list[str]:
    """Restriction labels where the substring oracle and the attributes differ.

    The alarm described in this module's docstring. Not consulted when serving
    deals -- it is surfaced at review time and logged at link time, because its
    job is catching human mislabelling, not vetoing curated data.
    """
    name = product.get("canonical_name") or ""
    out: list[str] = []
    for label in ("vegan", "vegetarian", "pescatarian", "no_nuts"):
        by_attrs = product_allows(product, [label])
        by_oracle = dietary.is_compliant(name, [label])
        if by_attrs != by_oracle:
            out.append(label)
    return out


def compliant_product_filter(restrictions: Iterable[str] | None) -> tuple[str, dict]:
    """SQL fragment + params restricting a ``deals d`` query to safe products.

    Returns ``("", {})`` when the user has no restrictions, so the caller can
    skip the join entirely. Otherwise the fragment requires a linked,
    *confirmed* product whose attributes are all False for the restriction --
    an EXISTS, so it composes with the caller's existing WHERE and applies
    before any LIMIT.
    """
    required = restrictions_to_attributes(restrictions)
    if not required:
        return "", {}
    # Attribute names come from our own constant, never from user input.
    for attr in required:
        assert attr in ATTRIBUTES, f"unknown attribute {attr!r}"
    conditions = " AND ".join(f"p.{attr} IS NOT TRUE" for attr in required)
    fragment = f"""
          AND EXISTS (
              SELECT 1 FROM products p
              WHERE p.product_id = d.product_id
                AND p.status = 'confirmed'
                AND {conditions}
          )"""
    return fragment, {}


# --------------------------------------------------------------------------
# Linking: deterministic, no LLM, idempotent
# --------------------------------------------------------------------------

def link_deals(database=db) -> dict[str, int]:
    """Point every deal at its confirmed product, where one exists.

    Normalisation happens in Python, not SQL, so there is exactly one
    implementation of :func:`normalize_name` and the two can never drift.
    Reads the distinct unlinked names, normalises them here, and updates by
    name. Idempotent: re-running links nothing new and changes no row.
    """
    stats = {"examined": 0, "linked_deals": 0, "linked_snapshots": 0, "unmapped": 0}

    with database.get_cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT product_name FROM deals WHERE product_id IS NULL"
        )
        names = [r["product_name"] for r in cursor.fetchall()]
        stats["examined"] = len(names)
        if not names:
            return stats

        cursor.execute(
            """
            SELECT normalized_name, product_id
            FROM product_aliases
            WHERE status = 'confirmed' AND product_id IS NOT NULL
            """
        )
        alias_map = {r["normalized_name"]: r["product_id"] for r in cursor.fetchall()}

        # Group raw names by the product they resolve to, so each product is
        # one UPDATE with an ANY(array) rather than one statement per string.
        by_product: dict[int, list[str]] = {}
        for raw in names:
            product_id = alias_map.get(normalize_name(raw))
            if product_id is None:
                stats["unmapped"] += 1
                continue
            by_product.setdefault(product_id, []).append(raw)

        for product_id, raws in by_product.items():
            cursor.execute(
                """
                UPDATE deals SET product_id = %(pid)s
                WHERE product_id IS NULL AND product_name = ANY(%(raws)s)
                """,
                {"pid": product_id, "raws": raws},
            )
            stats["linked_deals"] += cursor.rowcount or 0

            # Snapshots share the raw ingest string with deals, so the same
            # mapping applies. Linking them is what lets price_baseline
            # aggregate a product instead of one exact string.
            cursor.execute(
                """
                UPDATE price_snapshots SET product_id = %(pid)s
                WHERE product_id IS NULL AND product_name = ANY(%(raws)s)
                """,
                {"pid": product_id, "raws": raws},
            )
            stats["linked_snapshots"] += cursor.rowcount or 0

    if stats["linked_deals"]:
        logger.info(
            "Linked %d deal(s) and %d snapshot(s) to products; %d name(s) remain "
            "unmapped.",
            stats["linked_deals"],
            stats["linked_snapshots"],
            stats["unmapped"],
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
            """
            SELECT product_id, canonical_name, category, is_pantry_staple,
                   contains_meat, contains_fish, contains_dairy,
                   contains_egg, contains_nuts
            FROM products
            WHERE status = 'confirmed'
            ORDER BY canonical_name
            """
        )
        return [dict(r) for r in cursor.fetchall()]


def log_oracle_disagreements(database=db) -> list[dict]:
    """Confirmed products whose attributes contradict the substring oracle.

    Run after review as a cheap audit. A disagreement is usually the oracle
    being wrong (that is why this table exists), but it is occasionally a
    mislabelled product, and the only way to tell is to look.
    """
    flagged = []
    for product in confirmed_products(database=database):
        labels = oracle_disagreement(product)
        if labels:
            flagged.append({"product": product, "labels": labels})
            logger.warning(
                "Product %r (id=%s): curated attributes disagree with the "
                "substring oracle for %s. Attributes govern; verify the labels.",
                product["canonical_name"],
                product["product_id"],
                labels,
            )
    return flagged


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
            """
            SELECT a.alias_id, a.normalized_name, a.raw_example, a.confidence,
                   a.proposal_reason, a.times_seen, a.source,
                   p.product_id, p.canonical_name, p.category,
                   p.status AS product_status, p.is_pantry_staple,
                   p.contains_meat, p.contains_fish, p.contains_dairy,
                   p.contains_egg, p.contains_nuts
            FROM product_aliases a
            LEFT JOIN products p ON p.product_id = a.product_id
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
        group["oracle_disagreement"] = oracle_disagreement(group)

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
    clean = {k: bool(v) for k, v in updates.items()}
    if not clean:
        return {}
    sets = ", ".join(f"{attr} = %({attr})s" for attr in clean)
    with database.get_cursor() as cursor:
        cursor.execute(
            f"UPDATE products SET {sets} WHERE product_id = %(pid)s",
            {**clean, "pid": product_id},
        )
    return clean


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
              (SELECT count(*) FROM deals WHERE product_id IS NOT NULL)
                AS deals_linked,
              (SELECT count(*) FROM deals) AS deals_total
            """
        )
        return dict(cursor.fetchone())
