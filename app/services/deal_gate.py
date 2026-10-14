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
