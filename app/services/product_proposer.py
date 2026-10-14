"""Propose product mappings for review. Proposals are data pending review.

The contract, which is the whole design
---------------------------------------
This module **never** decides anything a user experiences. It writes rows with
``status='proposed'``, and ``app/services/products.py`` only ever links through
``status='confirmed'``. A human promotes proposals via
``scripts/review_products.py``. Request-time compliance stays a deterministic
read of curated columns.

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
``dietary.py`` and the existing catalogue for context instead of being handed a
prompt through a pinhole.

So the seam is a file, in three steps::

    scripts/propose_products.py --export unmapped.json   # 1. what needs labels
    (a Claude session fills in the labels -> labelled.json)
    scripts/propose_products.py --import labelled.json   # 2. persist as proposed
    scripts/review_products.py --review                  # 3. a human confirms

Step 2 validates every entry against :data:`PROPOSAL_SCHEMA` before it touches
the database, because a labelling step that is a conversation rather than a
constrained API response has no schema enforcement of its own. A malformed or
incomplete entry is rejected, never defaulted into something permissive.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.services.products import ATTRIBUTES, normalize_name

logger = logging.getLogger(__name__)

#: Shape every labelled entry must satisfy. Enforced by :func:`validate_payload`
#: on import -- this is a real gate, not documentation.
PROPOSAL_SCHEMA: dict[str, Any] = {
    "required": [
        "raw_name",
        "match_existing_product",
        "canonical_name",
        "category",
        "contains_meat",
        "contains_fish",
        "contains_dairy",
        "contains_egg",
        "contains_nuts",
        "is_pantry_staple",
        "confidence",
        "reason",
        "unmappable",
    ],
    "types": {
        "raw_name": str,
        "match_existing_product": (str, type(None)),
        "canonical_name": str,
        "category": str,
        "contains_meat": bool,
        "contains_fish": bool,
        "contains_dairy": bool,
        "contains_egg": bool,
        "contains_nuts": bool,
        "is_pantry_staple": bool,
        "confidence": (int, float),
        "reason": str,
        "unmappable": bool,
    },
}

#: Handed to the labeller in the export bundle. These are not decoration: every
#: case listed is one where the substring oracle or a naive reading gets the
#: answer wrong, and they are the reason a human reviews the output at all.
LABELLING_INSTRUCTIONS = """\
You are labelling raw grocery-flyer product strings for a meal-planning app. \
Your labels are reviewed by a human before anything uses them, so be explicit \
about uncertainty rather than confident and wrong.

For each string in `unmapped`, decide whether it is one of the products in \
`existing_products`, or a new product. Then set the dietary attributes of the \
PRODUCT -- what the food actually contains, not what words appear in the string.

Attribute rules:
- contains_meat: any mammal or bird flesh, including cured and processed forms.
- contains_fish: any fish OR shellfish.
- contains_dairy: milk-derived, including butter, cheese, cream, yogurt, whey.
- contains_egg: egg or egg-derived.
- contains_nuts: tree nuts or peanuts, including derived pastes and flours.
- is_pantry_staple: shelf-stable basics bought by the bag/box (flour, rice, \
sugar, oil, pasta, canned goods).

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
- A string naming two unrelated products, or pure marketing copy, is unmappable: \
set unmappable true and leave the attributes conservative (true).

canonical_name must be a short generic name: lowercase, no brand, no size. \
"chicken breast", not "Maple Leaf Prime Boneless Chicken Breasts 500g". Set \
match_existing_product to an existing canonical_name when the item IS that \
product, else null.

When you cannot tell, set unmappable true, or set the attribute to true and say \
why you are unsure in `reason`. A false "safe" label can expose an allergic \
person; a false "unsafe" label only costs them a deal."""


@dataclass
class Proposal:
    """One proposal for one normalised name."""

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


def export_bundle(
    names: Sequence[str], existing: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """The hand-off file: what needs labelling, plus everything needed to do it."""
    return {
        "instructions": LABELLING_INSTRUCTIONS,
        "output_format": {
            "labels": [
                {
                    "raw_name": "<copy the string from unmapped, verbatim>",
                    "match_existing_product": "<existing canonical_name, or null>",
                    "canonical_name": "<short generic name, lowercase>",
                    "category": "<e.g. produce, dairy, meat, bakery, pantry>",
                    **{attr: "<true|false>" for attr in ATTRIBUTES},
                    "is_pantry_staple": "<true|false>",
                    "confidence": "<0.0-1.0>",
                    "reason": "<one or two sentences a reviewer can check>",
                    "unmappable": "<true|false>",
                }
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


def validate_payload(payload: Any) -> list[str]:
    """Problems with one labelled entry. Empty list means it is usable.

    Strict on purpose. The labelling step is a conversation, not a
    schema-constrained API response, so this is the only thing standing between
    a malformed entry and an allergen attribute defaulting to something
    permissive.
    """
    if not isinstance(payload, dict):
        return ["not an object"]

    problems: list[str] = []
    for key in PROPOSAL_SCHEMA["required"]:
        if key not in payload:
            problems.append(f"missing {key!r}")
    for key, expected in PROPOSAL_SCHEMA["types"].items():
        if key not in payload:
            continue
        value = payload[key]
        # bool is a subclass of int; keep them from satisfying each other.
        if expected in ((int, float),) and isinstance(value, bool):
            problems.append(f"{key!r} must be a number, got bool")
            continue
        if expected is bool and not isinstance(value, bool):
            problems.append(f"{key!r} must be true/false, got {type(value).__name__}")
            continue
        if not isinstance(value, expected):
            name = getattr(expected, "__name__", str(expected))
            problems.append(f"{key!r} must be {name}, got {type(value).__name__}")

    if not problems:
        if not str(payload["raw_name"]).strip():
            problems.append("'raw_name' is blank")
        if not 0.0 <= float(payload["confidence"]) <= 1.0:
            problems.append("'confidence' must be between 0 and 1")
        if not payload["unmappable"] and not str(payload["canonical_name"]).strip():
            if not payload["match_existing_product"]:
                problems.append(
                    "needs a canonical_name or match_existing_product unless unmappable"
                )
    return problems


def proposal_from_payload(payload: dict[str, Any]) -> Proposal:
    """Build a Proposal from a *validated* payload."""
    raw = str(payload["raw_name"])
    return Proposal(
        normalized_name=normalize_name(raw),
        raw_example=raw,
        match_existing_product=payload.get("match_existing_product") or None,
        canonical_name=str(payload.get("canonical_name") or "").strip().lower(),
        category=str(payload.get("category") or "unknown").strip().lower(),
        # Conservative default. An attribute that somehow got past validation
        # must read as "contains it", never as safe.
        attributes={attr: bool(payload.get(attr, True)) for attr in ATTRIBUTES},
        is_pantry_staple=bool(payload.get("is_pantry_staple", False)),
        confidence=float(payload.get("confidence", 0.0) or 0.0),
        reason=str(payload.get("reason") or "").strip(),
        unmappable=bool(payload.get("unmappable", False)),
    )


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
        load.proposals.append(proposal_from_payload(payload))

    if expected_set is not None:
        load.unlabelled = sorted(expected_set - seen)

    return load


# --------------------------------------------------------------------------
# Persistence: everything lands as 'proposed'
# --------------------------------------------------------------------------

def persist_proposals(proposals: Iterable[Proposal], *, database) -> dict[str, int]:
    """Write proposals as ``status='proposed'`` rows. Affects no user yet.

    A proposal matching an existing product creates only an alias. A
    new-product proposal creates a proposed product *and* a proposed alias --
    confirming the alias is what makes both live, so the reviewer makes one
    decision, not two.

    Re-seeing a name bumps ``times_seen`` instead of inserting a duplicate, so
    a weekly re-ingest does not re-fill the queue.
    """
    stats = {"aliases_new": 0, "aliases_seen_again": 0, "products_proposed": 0, "skipped": 0}

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
                        """
                        INSERT INTO products (
                            canonical_name, category,
                            contains_meat, contains_fish, contains_dairy,
                            contains_egg, contains_nuts, is_pantry_staple,
                            status, proposed_by, proposal_reason
                        ) VALUES (
                            %(name)s, %(category)s,
                            %(contains_meat)s, %(contains_fish)s, %(contains_dairy)s,
                            %(contains_egg)s, %(contains_nuts)s, %(staple)s,
                            'proposed', %(by)s, %(reason)s
                        )
                        RETURNING product_id
                        """,
                        {
                            "name": proposal.canonical_name,
                            "category": proposal.category,
                            **proposal.attributes,
                            "staple": proposal.is_pantry_staple,
                            "by": "claude-session",
                            "reason": proposal.reason,
                        },
                    )
                    target_id = cursor.fetchone()["product_id"]
                    known[proposal.canonical_name] = {
                        "product_id": target_id,
                        "status": "proposed",
                    }
                    stats["products_proposed"] += 1

            cursor.execute(
                """
                INSERT INTO product_aliases (
                    normalized_name, raw_example, product_id,
                    status, source, confidence, proposal_reason
                ) VALUES (
                    %(norm)s, %(raw)s, %(pid)s, 'proposed', %(by)s, %(conf)s, %(reason)s
                )
                ON CONFLICT (normalized_name) DO UPDATE
                SET times_seen = product_aliases.times_seen + 1
                RETURNING times_seen
                """,
                {
                    "norm": proposal.normalized_name,
                    "raw": proposal.raw_example[:255],
                    "pid": target_id,
                    "by": "claude-session",
                    "conf": round(max(0.0, min(1.0, proposal.confidence)), 2),
                    "reason": proposal.reason,
                },
            )
            row = cursor.fetchone()
            if row and row["times_seen"] > 1:
                stats["aliases_seen_again"] += 1
            else:
                stats["aliases_new"] += 1

    return stats
