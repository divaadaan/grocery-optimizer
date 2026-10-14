"""Gate: which deal sources may reach recipe generation for which users.

============================================================================
KNOWN, ACCEPTED ALLERGEN-SAFETY GAP — DELIBERATELY DEFERRED, NOT AN OVERSIGHT
============================================================================
``app/agents/dietary.py`` is the deterministic dietary-compliance oracle. It
decides compliance by **casefold substring match** against a fixed vocabulary,
and its own docstring states the precondition it relies on:

    "No current seed/deal item collides; tighten to token boundaries if the
    catalog grows one that does."

Real flyer product names break that precondition **in both directions**:

* *False positives* (safe food wrongly rejected) — "Beefsteak Tomatoes"
  contains ``beef``; "Hamburger Buns" contains ``ham``; "Butternut Squash"
  contains both ``butter`` and ``nut``; "Graham Crackers" contains ``ham``.
  Cost: a vegetarian loses legitimate produce from their plan. Annoying.

* *False negatives* (forbidden food wrongly allowed) — **this is the
  dangerous direction**. ``prosciutto``, ``chorizo``, ``kielbasa``,
  ``capicollo``, ``pancetta``, ``gelatin``, ``lard``, ``marzipan``, ``tahini``
  and ``praline`` contain **no** term in the vocabulary. A nut-allergic user
  asking for a nut-free plan can be handed marzipan. That is an allergen
  exposure, not a UX annoyance.

The seed catalog was hand-written to avoid both. Real ingested flyer names are
not, so switching the deal source silently converts a sound oracle into an
unsound one.

The owner has decided to **ship ingestion now and fix the oracle later** (the
fix is token-boundary matching plus a ``products`` table with an explicit
allergen taxonomy — out of scope here, and deliberately NOT attempted in this
change). This module is the gate that makes that deferral safe rather than
silent:

    A user with ANY dietary restriction never sees ingested deals until
    ``INGESTED_DEALS_FOR_RESTRICTED_DIETS`` is explicitly turned on.

Both flags default **off**. Unrestricted users (``dietary_restrictions == []``)
are unaffected: the oracle has nothing to decide for them, so ingested deals
reach them as soon as ``DEAL_INGESTION_ENABLED`` is on.

**Do not flip ``INGESTED_DEALS_FOR_RESTRICTED_DIETS`` to true until
``app/agents/dietary.py`` matches on token boundaries and has an allergen
vocabulary that covers the cured-meat / hidden-animal-product / nut-derivative
tail above.** Flipping it is the act of accepting allergen risk on behalf of
users; it is not a performance tuning knob.

THE FIX HAS LANDED — PREFER ``PRODUCTS_CATALOG_ENABLED``
--------------------------------------------------------
``app/services/products.py`` plus migrations 006-008 replace the substring
oracle with curated per-product attributes, which is the proper fix rather
than the deferral this module was written to make safe. When
``PRODUCTS_CATALOG_ENABLED`` is on, a restricted user is served deals linked to
a **confirmed** product whose attributes clear their restrictions, regardless
of source — so a real ingested deal can reach them safely, which the
source-based gate could never allow.

The two flags are not additive, and the products gate is strictly the stronger
one, so it wins outright:

* ``PRODUCTS_CATALOG_ENABLED`` on  -> product-backed gate.
  ``INGESTED_DEALS_FOR_RESTRICTED_DIETS`` is **ignored**, because "trust every
  ingested name" and "trust only confirmed products" cannot both be in force
  and the second is the safe one. A restricted user sees only confirmed-product
  deals even with the older flag on.
* ``PRODUCTS_CATALOG_ENABLED`` off -> the original source-based gate below,
  with the deferral and its caveats exactly as described above.

Note what the products gate does *not* need: the oracle. Keeping it as a veto
would reintroduce the false positives (a product a curator named "beefsteak
tomato" would be withheld from vegetarians) while adding no protection on the
false negatives that motivated all of this — ``marzipan`` contains no nut
term, so the oracle never catches it. It is kept as a review-time alarm
instead; see ``products.oracle_disagreement``.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from app.config import settings

logger = logging.getLogger(__name__)

#: Deal sources whose product names were curated by hand, and which
#: ``app/agents/dietary.py``'s substring oracle is therefore sound against.
#: ``'seed'`` is ``scripts/seed_sample_data.sql``; ``'manual'`` is reserved for
#: hand-entered rows. Anything else is machine-ingested and untrusted for
#: dietary decisions.
CURATED_SOURCES: frozenset[str] = frozenset({"seed", "manual"})


def restrictions_are_active(restrictions: Iterable[str] | None) -> bool:
    """True if the user has at least one non-blank dietary restriction."""
    if not restrictions:
        return False
    return any((r or "").strip() for r in restrictions)


def ingested_deals_allowed(
    restrictions: Iterable[str] | None,
    *,
    allow_for_restricted: bool | None = None,
) -> bool:
    """May machine-ingested deals be shown to this user?

    Yes if they have no dietary restrictions (nothing for the unsound oracle to
    get wrong), or if the deferral has been explicitly waived in config.
    """
    if allow_for_restricted is None:
        allow_for_restricted = settings.ingested_deals_for_restricted_diets
    if not restrictions_are_active(restrictions):
        return True
    return bool(allow_for_restricted)


#: The three gate modes. ``OPEN`` means no filtering is needed at all.
GATE_OPEN = "open"
GATE_PRODUCTS = "products"
GATE_CURATED_SOURCES = "curated_sources"


def gate_mode(
    restrictions: Iterable[str] | None,
    *,
    products_enabled: bool | None = None,
    allow_for_restricted: bool | None = None,
) -> str:
    """Which gate applies to this user. See the module docstring for the why.

    Precedence is deliberate and not a free choice: the products gate is the
    stronger guarantee, so it outranks the source gate whenever it is on.
    """
    if products_enabled is None:
        products_enabled = settings.products_catalog_enabled

    if not restrictions_are_active(restrictions):
        return GATE_OPEN
    if products_enabled:
        return GATE_PRODUCTS
    if ingested_deals_allowed(restrictions, allow_for_restricted=allow_for_restricted):
        return GATE_OPEN
    return GATE_CURATED_SOURCES


def allowed_sources(
    restrictions: Iterable[str] | None,
    *,
    allow_for_restricted: bool | None = None,
) -> list[str] | None:
    """Sources this user may be served, or ``None`` for "no restriction".

    ``None`` means every source is allowed, so the caller can skip the SQL
    filter entirely. A list means filter ``deals.source`` to exactly these.
    """
    if ingested_deals_allowed(restrictions, allow_for_restricted=allow_for_restricted):
        return None
    return sorted(CURATED_SOURCES)


def filter_deals(
    deals: Sequence[dict[str, Any]],
    restrictions: Iterable[str] | None,
    *,
    allow_for_restricted: bool | None = None,
) -> list[dict[str, Any]]:
    """In-memory counterpart to :func:`allowed_sources`.

    Used where deals have already been fetched. Rows with no ``source`` key at
    all are treated as curated — that is the pre-migration shape of the
    ``deals`` table, and failing closed on it would empty the seed catalog for
    every restricted user.
    """
    permitted = allowed_sources(restrictions, allow_for_restricted=allow_for_restricted)
    if permitted is None:
        return list(deals)

    kept = [d for d in deals if (d.get("source") or "seed") in permitted]
    dropped = len(deals) - len(kept)
    if dropped:
        logger.warning(
            "Deal gate: withheld %d ingested deal(s) from a user with restrictions "
            "%s. app/agents/dietary.py's substring oracle is not safe against raw "
            "flyer names (see app/services/deal_gate.py); set "
            "INGESTED_DEALS_FOR_RESTRICTED_DIETS=true only after it is fixed.",
            dropped,
            list(restrictions or []),
        )
    return kept
