"""Propose product mappings for review. Proposals are data pending review.

The contract, which is the whole design
---------------------------------------
This module **never** decides anything a user experiences. It writes rows with
``status='proposed'``, and ``app/services/products.py`` only ever links through
``status='confirmed'``. A human promotes proposals via
``scripts/review_products.py`` or ``/admin/review``. Request-time compliance
stays a deterministic read of curated columns.

That separation is not ceremony. This project already spent an entire
supervised-fine-tuning track establishing that LLMs get dietary compliance
wrong in the dangerous direction -- small models false-rejected 100% of
compliant recipes while confabulating justifications, and scaling 1.5B -> 8B
moved the metric by exactly zero. The conclusion was that compliance must be a
deterministic code path. A model proposing *labels for a human to check* does
not violate that conclusion; a model deciding at request time would.

Why there is no API client here
-------------------------------
The labelling is done by whichever Claude session is already working in this
repo, not by an outbound API call. The app needs no ``ANTHROPIC_API_KEY``, no
SDK dependency, and no network egress to categorise its own data -- and
labelling in-session means the labeller can read ``seed_catalog.py``,
``products.RESTRICTION_ATTRIBUTES`` and the existing catalogue for context
instead of being handed a prompt through a pinhole.

So the seam is a file, in three steps::

    scripts/propose_products.py --export unmapped.json   # 1. what needs labels
    (a Claude session fills in the labels -> labelled.json)
    scripts/propose_products.py --import labelled.json   # 2. persist as proposed
    scripts/review_products.py --review                  # 3. a human confirms

Step 2 validates every entry before it touches the database, because a
labelling step that is a conversation rather than a constrained API response
has no schema enforcement of its own. A malformed or incomplete entry is
rejected, never defaulted into something permissive.

Multi-product tiles
-------------------
A flyer tile can advertise several unrelated products at one price. Such an
entry carries a ``products`` list instead of flat fields, and yields one
Proposal per product -- see :func:`proposals_from_payload`. The single-product
flat form is still valid and is the common case.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.services.products import ATTRIBUTES, normalize_name

logger = logging.getLogger(__name__)

#: Fields every labelled entry must carry, whatever its shape.
ENTRY_REQUIRED = ("raw_name", "confidence", "reason", "unmappable")

#: Fields describing ONE product. Required on a flat entry, and on each member
#: of a ``products`` list.
PRODUCT_REQUIRED = (
    "match_existing_product",
    "canonical_name",
    "category",
    *ATTRIBUTES,
    "is_pantry_staple",
)

TYPES: dict[str, Any] = {
    "raw_name": str,
    "confidence": (int, float),
    "reason": str,
    "unmappable": bool,
    "match_existing_product": (str, type(None)),
    "canonical_name": str,
    "category": str,
    "is_pantry_staple": bool,
    "tile_position": int,
    "price_is_per_item": (bool, type(None)),
    **{attr: bool for attr in ATTRIBUTES},
}

#: Kept for callers and tests that want the full single-product field list.
PROPOSAL_SCHEMA: dict[str, Any] = {
    "required": [*ENTRY_REQUIRED, *PRODUCT_REQUIRED],
    "types": TYPES,
}

LABELLING_INSTRUCTIONS = """\
You are labelling raw grocery-flyer product strings for a meal-planning app. \
Your labels are reviewed by a human before anything uses them, so be explicit \
about uncertainty rather than confident and wrong.

For each string in `unmapped`, decide whether it is one of the products in \
`existing_products`, or a new product. Then set the dietary attributes of the \
PRODUCT -- what the food actually contains, not what words appear in the string.

Attribute rules:
- contains_meat: any mammal or bird flesh, including cured and processed forms, \
AND slaughter by-products that vegetarians also avoid: gelatin, lard, tallow, \
animal rennet.
- contains_fish: any fish OR shellfish.
- contains_dairy: milk-derived, including butter, cheese, cream, yogurt, whey.
- contains_egg: egg or egg-derived.
- contains_nuts: tree nuts or peanuts, including derived pastes and flours.
- contains_animal_byproduct: an animal-derived ingredient that is NOT meat, \
fish, dairy or egg -- honey, beeswax, carmine/cochineal (red colouring, E120), \
shellac/confectioner's glaze, bone-char-refined sugar, lanolin-derived vitamin \
D3. Only vegans avoid these; vegetarians eat honey. LABELLING RULE: gelatin, \
lard, tallow and animal rennet are contains_meat, NOT this attribute, because \
vegetarians avoid them too. A product with honey is contains_animal_byproduct \
true and contains_meat false.
- is_pantry_staple: shelf-stable basics bought by the bag/box (flour, rice, \
sugar, oil, pasta, canned goods).

MULTI-PRODUCT TILES. A flyer tile often advertises several unrelated products \
at one price, e.g. "ANNIE'S SHELLS & CHEDDAR 170 G BEATRICE CHOCOLATE MILK 1 L \
GOLDFISH CRACKERS 168 - 227 G" is three products. For those, return a \
`products` list with one entry per product instead of the flat fields, and set \
`price_is_per_item` on each:
  - true  when the tile prices each item separately. The `unit` field on the \
deal is the signal: "each", "ea.", "lb" mean per-item.
  - false for a bundle or multi-buy ("2 for $5"), where the price is NOT any \
one product's price.
  - null when you cannot tell. Null is a real answer and better than a guess; \
the link step refuses to use it rather than defaulting.

Do NOT split a tile that lists VARIANTS of one product. These are ONE product:
  - "NEILSON TRUTASTE 1%, 2% OR SKIM MILK, 4 L"  -> one product, three fat levels
  - "Natrel Fine-Filtered Milk 3.25%, 2%, 1% or Skim" -> one product
  - "MAPLE LEAF ... CHICKEN BREAST FILLETS OR CUTLETS" -> one product, two cuts
These are TWO OR MORE products:
  - "MARC ANGELO CHICKEN BREAST BITES OR PORK MEATBALLS"
  - "NEILSON CREAM or MILK2GO"
  - "ORGANIC COCONUT MILK OR CREAM"
The test is whether a shopper would call them the same thing with a different \
label on it, or different things.

Cases that are commonly got wrong -- these are why a human reviews you:
- "Beefsteak Tomatoes" is a TOMATO. A vegetable. No animal products. "beef" is \
a varietal name.
- "Hamburger Buns" is BREAD. The "ham" is incidental.
- "Oyster Mushrooms" is a MUSHROOM, not shellfish.
- "prosciutto", "capicollo", "pancetta", "kielbasa", "chorizo", "bresaola", \
"mortadella" are all pork or beef: contains_meat is true even though no obvious \
meat word appears.
- "gelatin" and "lard" are animal-derived: contains_meat is true.
- "marzipan", "praline", "frangipane", "nutella" contain nuts.
- "pesto" usually contains pine nuts AND cheese.
- Worcestershire and many fish sauces contain anchovy: contains_fish.
- Vegan analogues ("Vegan Chicken Strips", "Fishless Fingers") contain NO animal \
product. Attributes describe the food, not the name.
- COCONUT IS NOT A NUT for this app. contains_nuts is false for coconut milk, \
coconut cream, coconut oil and shredded coconut. The FDA classifies coconut as \
a tree nut for labelling purposes, but coconut allergy is rare and largely \
unrelated to tree-nut allergy, and treating it as a nut would withhold a \
staple from every nut-free user for no benefit. Decided 2026-10-14; do not \
re-litigate it per item.
- Honey, honey-sweetened products ("honey garlic sauce", "honey oat bread") \
and anything glazed with shellac are contains_animal_byproduct true. \
White and brown cane sugar of unknown origin may be bone-char refined: \
contains_animal_byproduct true unless the product says beet, organic or \
certified vegan. Candy and yogurt coloured with carmine are true.
- Lactose-free milk is STILL DAIRY. Lactose is removed, not the milk: \
contains_dairy is true, so it fails vegan.
- A string that is pure marketing copy, or so garbled that the products cannot \
be identified, is unmappable: set unmappable true and leave attributes \
conservative (true).

canonical_name must be a short generic name: lowercase, no brand, no size. \
"chicken breast", not "Maple Leaf Prime Boneless Chicken Breasts 500g". Set \
match_existing_product to an existing canonical_name when the item IS that \
product, else null. In `existing_products`, null for an attribute means a \
human has not decided it yet; it is not false. Your own labels must always be \
true or false.

When you cannot tell, set unmappable true, or set the attribute to true and say \
why you are unsure in `reason`. A false "safe" label can expose an allergic \
person; a false "unsafe" label only costs them a deal."""


@dataclass
class Proposal:
    """One proposal: this normalised string refers to this product.

    A multi-product tile yields several of these, distinguished by
    ``tile_position``.
    """

    normalized_name: str
    raw_example: str
    match_existing_product: str | None
    canonical_name: str
    category: str
    attributes: dict[str, bool]
    is_pantry_staple: bool
    confidence: float
    reason: str
    unmappable: bool
    tile_position: int = 0
    #: Whether the deal's sale_price is this product's own price. None means
    #: undecided; the link step refuses to use it rather than guessing.
    price_is_per_item: bool | None = None


@dataclass
class LabelLoad:
    """Outcome of reading a labelled file.

    ``unlabelled`` is deliberately **not** an error. Exports are limited
    (``--limit``), and labelling a subset is the normal way to work through a
    long queue, so a name with no label just stays withheld until someone gets
    to it. Only malformed entries and invented names are errors.
    """

    proposals: list[Proposal] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    unlabelled: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def tiles(self) -> int:
        """How many source strings produced more than one product."""
        counts: dict[str, int] = {}
        for p in self.proposals:
            counts[p.raw_example] = counts.get(p.raw_example, 0) + 1
        return sum(1 for n in counts.values() if n > 1)


def export_bundle(
    names: Sequence[str], existing: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """The hand-off file: what needs labelling, plus everything needed to do it."""
    one_product = {
        "match_existing_product": "<existing canonical_name, or null>",
        "canonical_name": "<short generic name, lowercase>",
        "category": "<e.g. produce, dairy, meat, bakery, pantry>",
        **{attr: "<true|false>" for attr in ATTRIBUTES},
        "is_pantry_staple": "<true|false>",
    }
    return {
        "instructions": LABELLING_INSTRUCTIONS,
        "output_format": {
            "labels": [
                {
                    "_comment": "single product: flat fields",
                    "raw_name": "<copy the string from unmapped, verbatim>",
                    **one_product,
                    "confidence": "<0.0-1.0>",
                    "reason": "<one or two sentences a reviewer can check>",
                    "unmappable": "<true|false>",
                },
                {
                    "_comment": (
                        "multi-product tile: a products list instead of flat "
                        "fields, one entry per product"
                    ),
                    "raw_name": "<copy the string from unmapped, verbatim>",
                    "products": [
                        {**one_product, "price_is_per_item": "<true|false|null>"},
                    ],
                    "confidence": "<0.0-1.0>",
                    "reason": "<why you split it this way>",
                    "unmappable": False,
                },
            ]
        },
        "existing_products": [
            {
                "canonical_name": p["canonical_name"],
                "category": p.get("category"),
                **{attr: p.get(attr) for attr in ATTRIBUTES},
            }
            for p in existing
        ],
        "unmapped": list(names),
    }


def _check_types(payload: dict[str, Any], keys: Iterable[str]) -> list[str]:
    problems: list[str] = []
    for key in keys:
        if key not in payload:
            continue
        expected = TYPES.get(key)
        if expected is None:
            continue
        value = payload[key]
        # bool subclasses int; keep them from satisfying each other.
        if expected == (int, float) and isinstance(value, bool):
            problems.append(f"{key!r} must be a number, got bool")
            continue
        if expected is bool and not isinstance(value, bool):
            problems.append(f"{key!r} must be true/false, got {type(value).__name__}")
            continue
        if not isinstance(value, expected):
            name = getattr(expected, "__name__", str(expected))
            problems.append(f"{key!r} must be {name}, got {type(value).__name__}")
    return problems


def _validate_product(spec: Any, where: str) -> list[str]:
    if not isinstance(spec, dict):
        return [f"{where}: not an object"]
    problems = [f"{where}: missing {k!r}"
                for k in PRODUCT_REQUIRED if k not in spec]
    problems += [f"{where}: {p}" for p in _check_types(spec, PRODUCT_REQUIRED)]
    problems += [f"{where}: {p}" for p in _check_types(spec, ("price_is_per_item",))]
    if not problems and not str(spec["canonical_name"]).strip():
        if not spec["match_existing_product"]:
            problems.append(
                f"{where}: needs a canonical_name or match_existing_product"
            )
    return problems


def validate_payload(payload: Any) -> list[str]:
    """Problems with one labelled entry. Empty list means it is usable.

    Strict on purpose. The labelling step is a conversation, not a
    schema-constrained API response, so this is the only thing standing between
    a malformed entry and an allergen attribute defaulting to something
    permissive.
    """
    if not isinstance(payload, dict):
        return ["not an object"]

    problems = [f"missing {k!r}" for k in ENTRY_REQUIRED if k not in payload]
    problems += _check_types(payload, ENTRY_REQUIRED)
    if problems:
        return problems

    if not str(payload["raw_name"]).strip():
        problems.append("'raw_name' is blank")
    if not 0.0 <= float(payload["confidence"]) <= 1.0:
        problems.append("'confidence' must be between 0 and 1")

    products = payload.get("products")
    if products is not None:
        if not isinstance(products, list) or not products:
            problems.append("'products' must be a non-empty list")
        else:
            if payload["unmappable"]:
                problems.append("'unmappable' cannot be true alongside 'products'")
            for index, spec in enumerate(products):
                problems += _validate_product(spec, f"products[{index}]")
            names = [
                str(s.get("match_existing_product") or s.get("canonical_name") or "")
                .strip().lower()
                for s in products if isinstance(s, dict)
            ]
            if len(set(names)) != len(names):
                problems.append("'products' names the same product twice")
    elif not payload["unmappable"]:
        problems += _validate_product(payload, "entry")

    return problems


def proposals_from_payload(payload: dict[str, Any]) -> list[Proposal]:
    """One Proposal per product. A *validated* payload yields at least one."""
    raw = str(payload["raw_name"])
    normalized = normalize_name(raw)
    common = {
        "normalized_name": normalized,
        "raw_example": raw,
        "confidence": float(payload.get("confidence", 0.0) or 0.0),
        "reason": str(payload.get("reason") or "").strip(),
    }

    def build(spec: dict[str, Any], position: int, unmappable: bool) -> Proposal:
        return Proposal(
            match_existing_product=spec.get("match_existing_product") or None,
            canonical_name=str(spec.get("canonical_name") or "").strip().lower(),
            category=str(spec.get("category") or "unknown").strip().lower(),
            # Conservative default. An attribute that somehow got past
            # validation must read as "contains it", never as safe.
            attributes={attr: bool(spec.get(attr, True)) for attr in ATTRIBUTES},
            is_pantry_staple=bool(spec.get("is_pantry_staple", False)),
            unmappable=unmappable,
            tile_position=position,
            price_is_per_item=spec.get("price_is_per_item"),
            **common,
        )

    products = payload.get("products")
    if products:
        return [build(spec, i, False) for i, spec in enumerate(products)]
    # A single-product entry is left UNDECIDED when the label omits the field.
    #
    # This used to default to True, reasoning that "a single product prices per
    # item by definition: there is nothing else on the tile for the price to be
    # shared with". That reasoning is wrong -- a single product is routinely
    # 2-for-$7, and the 5-term probe found nine such names (Dempster's sliced
    # breads and COMPLIMENTS Peanut Butter at $7.00, with pre_price_text '2/'
    # or '3/'). Worse, because reinfer_alias_prices only ever fills NULLs, a
    # wrong True was never corrected: a labeller who merely omitted the field
    # would have shipped halved prices permanently.
    #
    # None is the honest answer, and it is also the recoverable one --
    # reinfer_alias_prices fills it from the deal's unit / sale_story /
    # pre_price_text, and anything it cannot decide stays withheld rather than
    # priced wrongly.
    return [build(payload, 0, bool(payload.get("unmappable", False)))]


def load_labels(path: str | Path, *, expected: Iterable[str] | None = None) -> LabelLoad:
    """Read and validate a labelled file.

    ``expected`` is the set of names that were exported. Any label for a name
    that was not exported is rejected -- it means the labeller invented or
    altered a string, and a mismatched ``raw_name`` would link the wrong deals.
    """
    load = LabelLoad()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        load.errors["__file__"] = str(exc)
        return load

    entries = data.get("labels") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        load.errors["__file__"] = "expected a 'labels' list"
        return load

    expected_set = set(expected) if expected is not None else None
    seen: set[str] = set()

    for index, payload in enumerate(entries):
        problems = validate_payload(payload)
        if problems:
            key = (
                str(payload.get("raw_name"))
                if isinstance(payload, dict) and payload.get("raw_name")
                else f"entry[{index}]"
            )
            load.errors[key] = "; ".join(problems)
            continue

        raw = str(payload["raw_name"])
        if expected_set is not None and raw not in expected_set:
            load.errors[raw] = "not in the exported set (altered or invented name)"
            continue
        if raw in seen:
            load.errors[raw] = "duplicate label for the same name"
            continue
        seen.add(raw)
        load.proposals.extend(proposals_from_payload(payload))

    if expected_set is not None:
        load.unlabelled = sorted(expected_set - seen)

    return load


# --------------------------------------------------------------------------
# Persistence: everything lands as 'proposed'
# --------------------------------------------------------------------------

#: One name may map to many products (migration 010), so the conflict target is
#: the pairing. Both targets name a PARTIAL index, so the predicate has to be
#: repeated for Postgres to infer it.
#:
#: On conflict only ``times_seen`` and ``price_is_per_item`` move. Pricing is
#: filled when NULL and never overwritten: NULL is the absence of a decision (a
#: confirmed alias predating migration 010 has one), so filling it overrules
#: nobody. Status, confidence, reason and tile_position are deliberately left
#: alone -- those are review state, and a re-import must not rewrite them.
_ALIAS_UPSERT_PLACED = """
INSERT INTO product_aliases (
    normalized_name, raw_example, product_id, status, source,
    confidence, proposal_reason, tile_position, price_is_per_item
) VALUES (
    %(norm)s, %(raw)s, %(pid)s, 'proposed', %(by)s,
    %(conf)s, %(reason)s, %(pos)s, %(per_item)s
)
ON CONFLICT (normalized_name, product_id) WHERE product_id IS NOT NULL
DO UPDATE SET
    times_seen = product_aliases.times_seen + 1,
    price_is_per_item = COALESCE(
        product_aliases.price_is_per_item, EXCLUDED.price_is_per_item)
RETURNING times_seen
"""

_ALIAS_UPSERT_UNPLACED = """
INSERT INTO product_aliases (
    normalized_name, raw_example, product_id, status, source,
    confidence, proposal_reason, tile_position, price_is_per_item
) VALUES (
    %(norm)s, %(raw)s, NULL, 'proposed', %(by)s,
    %(conf)s, %(reason)s, %(pos)s, %(per_item)s
)
ON CONFLICT (normalized_name) WHERE product_id IS NULL
DO UPDATE SET
    times_seen = product_aliases.times_seen + 1,
    price_is_per_item = COALESCE(
        product_aliases.price_is_per_item, EXCLUDED.price_is_per_item)
RETURNING times_seen
"""

_PRODUCT_INSERT = """
INSERT INTO products (
    canonical_name, category,
    contains_meat, contains_fish, contains_dairy,
    contains_egg, contains_nuts, contains_animal_byproduct, is_pantry_staple,
    status, proposed_by, proposal_reason
) VALUES (
    %(name)s, %(category)s,
    %(contains_meat)s, %(contains_fish)s, %(contains_dairy)s,
    %(contains_egg)s, %(contains_nuts)s, %(contains_animal_byproduct)s, %(staple)s,
    'proposed', %(by)s, %(reason)s
)
RETURNING product_id
"""

SOURCE = "claude-session"


def persist_proposals(proposals: Iterable[Proposal], *, database) -> dict[str, int]:
    """Write proposals as ``status='proposed'`` rows. Affects no user yet.

    A proposal matching an existing product creates only an alias. A
    new-product proposal creates a proposed product *and* a proposed alias --
    confirming the alias is what makes both live, so the reviewer makes one
    decision, not two.

    Re-seeing a name bumps ``times_seen`` instead of inserting a duplicate, so
    a weekly re-ingest does not re-fill the queue. For a multi-product tile
    that is per (name, product), so each product of the tile is tracked
    separately.
    """
    stats = {"aliases_new": 0, "aliases_seen_again": 0, "products_proposed": 0,
             "skipped": 0, "tile_rows": 0,
             "unplaced_superseded": 0}

    with database.get_cursor() as cursor:
        cursor.execute(
            "SELECT lower(canonical_name) AS name, product_id, status FROM products"
        )
        known = {r["name"]: r for r in cursor.fetchall()}

        for proposal in proposals:
            if not proposal.normalized_name:
                stats["skipped"] += 1
                continue

            target_id: int | None = None
            match = (proposal.match_existing_product or "").strip().lower()
            if match and match in known:
                target_id = known[match]["product_id"]
            elif not proposal.unmappable and proposal.canonical_name:
                existing = known.get(proposal.canonical_name)
                if existing:
                    target_id = existing["product_id"]
                else:
                    cursor.execute(
                        _PRODUCT_INSERT,
                        {
                            "name": proposal.canonical_name,
                            "category": proposal.category,
                            **proposal.attributes,
                            "staple": proposal.is_pantry_staple,
                            "by": SOURCE,
                            "reason": proposal.reason,
                        },
                    )
                    target_id = cursor.fetchone()["product_id"]
                    known[proposal.canonical_name] = {
                        "product_id": target_id,
                        "status": "proposed",
                    }
                    stats["products_proposed"] += 1

            params = {
                "norm": proposal.normalized_name,
                "raw": proposal.raw_example[:255],
                "pid": target_id,
                "by": SOURCE,
                "conf": round(max(0.0, min(1.0, proposal.confidence)), 2),
                "reason": proposal.reason,
                "pos": proposal.tile_position,
                "per_item": proposal.price_is_per_item,
            }
            if target_id is None:
                params.pop("pid")
                cursor.execute(_ALIAS_UPSERT_UNPLACED, params)
            else:
                cursor.execute(_ALIAS_UPSERT_PLACED, params)

            row = cursor.fetchone()
            if row and row["times_seen"] > 1:
                stats["aliases_seen_again"] += 1
            else:
                stats["aliases_new"] += 1
            if proposal.tile_position > 0:
                stats["tile_rows"] += 1

            if target_id is not None:
                # A name that was previously unplaceable now has products. The
                # old unplaced row is superseded: left 'proposed' it would show
                # the same string in the review queue twice, once as a product
                # and once as "could not place this".
                cursor.execute(
                    """
                    UPDATE product_aliases
                    SET status = 'rejected',
                        reviewed_by = %(by)s,
                        proposal_reason = 'superseded by a decomposition into '
                                          'specific products'
                    WHERE normalized_name = %(norm)s
                      AND product_id IS NULL
                      AND status = 'proposed'
                    """,
                    {"norm": proposal.normalized_name, "by": SOURCE},
                )
                stats["unplaced_superseded"] += cursor.rowcount or 0

    return stats
