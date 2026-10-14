"""Staples-driven deal ingestion: Flipp flyer search -> ``deals`` + ``price_snapshots``.

Shape of the thing
------------------
``run_ingestion`` loops the staple terms (``app/services/flipp_staples.py``),
asks a :class:`~app.services.flipp_client.DealProvider` for each one, and
writes two things per surviving item:

1. a row in **deals** — the current offer, upserted on a natural key so
   re-running the same week is a no-op;
2. a row in **price_snapshots** — one observation per item per day, so the
   project accumulates its *own* price history instead of trusting a
   ``regular_price`` the source mostly does not send. ``price_snapshots`` had
   no Python writer before this module; see ``app/services/price_baseline.py``
   for the reader.

Normalisation is pure (``normalize_items``) and tested against a recorded
response; persistence is separate and needs a DB.

Honest limitations of the source data
------------------------------------
* **``original_price`` is present on ~14% of flyer items** (5/35 on the
  recorded probe). So ``deals.regular_price`` is usually NULL, and
  ``discount_percentage`` with it. ``estimated_savings = regular - sale``
  therefore degrades badly on real data. That is accepted: the hero metric is
  "can the user eat to their dietary goals inside their spend limit", and
  savings is secondary. Nothing here invents a regular price to keep the old
  metric looking healthy.
* **A flyer item has no store, only a merchant.** There is no address, no
  branch, no geocode — just ``merchant_name``/``merchant_id``. So an ingested
  ``stores`` row is a *merchant within a postal code* ("No Frills near
  M5V3A8"), not a physical location, and it is tagged ``source='flipp'`` to
  keep it distinguishable from the hand-written seed stores, which are real
  addresses.
* **Category is derived from the search term, not the payload.** Every
  grocery item comes back as ``_L1="Food, Beverages & Tobacco"`` /
  ``_L2="Food Items"``, which tells us nothing. So a hit for "rice" is filed
  under Pantry. ``_L1`` is still used as a *negative* filter — it reliably
  flags the non-grocery long tail (a "rice" search can return a rice cooker
  under ``Kitchen & Dining``).
* **``brand`` is never populated.** The payload carries ``brand_ids`` (opaque
  numeric ids) and no brand name. ``deals.brand`` stays NULL rather than
  holding a guess parsed out of ``name``.
* **``valid_until`` can be optimistic by up to a day.** ``valid_to`` arrives
  as a UTC instant that is local midnight (e.g. ``2026-10-15T03:59:59+00:00``
  is 23:59:59 Oct 14 in Toronto). We store its UTC date, because the payload
  gives us no store timezone to do better with. A deal can therefore read as
  active for one day past the flyer's last local day.

Schema dependency
-----------------
This module writes columns added by ``migrations/001``-``migrations/003``
(``source``, ``source_item_id``, ``source_merchant_id``, ``price_basis``, a
nullable ``price_snapshots.price``, and the natural-key unique indexes). Run
those before enabling ingestion or every INSERT will fail on an unknown column.

Snapshots land in ``price_snapshots_default``, because the named monthly
partitions stop at 2026-01 and nothing creates the current month. That works
(the DEFAULT partition is a catch-all) and it is also, today, what keeps the
price history safe from ``cleanup_old_price_partitions()`` — see
``migrations/005`` and ``../sql4e/DESIGN-LOG.md`` 0.5/0.6. Adding current
partitions is deliberately NOT done here: on any database that already has
rows in the default partition it fails with ERROR 23514, and the correct
DETACH/relocate/ATTACH fix is already scheduled.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Sequence

from app.db import db
from app.services.flipp_client import DealProviderError
from app.services.flipp_staples import category_for_term, staple_terms

logger = logging.getLogger(__name__)

# ``_L1`` values we accept. Anything else is the non-grocery long tail that a
# food search occasionally drags in (mattresses for "milk", rice cookers for
# "rice"). A missing/empty ``_L1`` is accepted — plenty of real flyer items
# carry no taxonomy at all and dropping them would cost real coverage.
FOOD_L1 = frozenset({"Food, Beverages & Tobacco"})

#: ``price_snapshots.price_basis`` values. The point of the column is to record
#: *which* number we actually saw rather than guessing, since the source
#: usually sends exactly one price and does not say what kind it is.
BASIS_SALE_ONLY = "sale_only"          # only the advertised promo price
BASIS_REGULAR_AND_SALE = "regular_and_sale"  # pre-promo price seen too


@dataclass(frozen=True)
class NormalizedDeal:
    """One flyer item, cleaned up and ready to persist."""

    product_name: str
    merchant_name: str
    merchant_id: str | None
    sale_price: float
    regular_price: float | None
    unit: str | None
    category: str | None
    valid_from: date
    valid_until: date
    source_item_id: str
    search_term: str
    sale_story: str | None = None

    @property
    def price_basis(self) -> str:
        return BASIS_REGULAR_AND_SALE if self.regular_price is not None else BASIS_SALE_ONLY

    @property
    def discount_percentage(self) -> int | None:
        """Integer discount, or None when there is no regular price to compare.

        Deliberately returns None rather than 0 when ``regular_price`` is
        missing: 0 would read as "no discount" and quietly pollute
        ``idx_deals_discount`` ordering and ``best_deals_by_category``.
        """
        if self.regular_price is None or self.regular_price <= 0:
            return None
        if self.regular_price <= self.sale_price:
            return None
        return int(round((self.regular_price - self.sale_price) / self.regular_price * 100))


@dataclass
class IngestionReport:
    """What one ingestion run actually did. Returned to the caller and logged."""

    postal_code: str
    source: str
    terms_searched: int = 0
    terms_failed: list[str] = field(default_factory=list)
    raw_items: int = 0
    normalized: int = 0
    skipped_no_name: int = 0
    skipped_no_price: int = 0
    skipped_non_food: int = 0
    skipped_bad_dates: int = 0
    # same provider item id returned by two different search terms
    duplicate_items: int = 0
    # same offer printed twice in one flyer under two different item ids
    duplicate_offers: int = 0
    sale_price_only: int = 0
    stores_created: int = 0
    deals_inserted: int = 0
    deals_updated: int = 0
    snapshots_inserted: int = 0
    snapshots_skipped: int = 0
    observed_on: datetime | None = None

    @property
    def regular_price_coverage(self) -> float:
        """Fraction of ingested deals that came with a pre-promo price.

        Surfaced because it is the number that decides whether
        ``estimated_savings`` means anything this week. Observed ~0.14.
        """
        if not self.normalized:
            return 0.0
        return round((self.normalized - self.sale_price_only) / self.normalized, 3)

    def as_dict(self) -> dict[str, Any]:
        out = {
            k: v for k, v in self.__dict__.items() if not k.startswith("_")
        }
        out["observed_on"] = self.observed_on.isoformat() if self.observed_on else None
        out["regular_price_coverage"] = self.regular_price_coverage
        return out


# ---------------------------------------------------------------------------
# Normalisation (pure — no DB, no network)
# ---------------------------------------------------------------------------

def _number(value: Any) -> float | None:
    """Coerce a provider price to float. ``current_price`` is int OR float."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().lstrip("$"))
        except ValueError:
            return None
    return None


def _clean_unit(raw: Any) -> str | None:
    """``post_price_text`` -> a unit string, or None.

    Observed values are a unit with inconsistent casing and an optional
    leading slash: "lb", "/lb", "LB", "/LB", "ea.", "/100 g", "each".
    Lowercased and de-slashed so they group; truncated to the column width.
    """
    if not isinstance(raw, str):
        return None
    unit = raw.strip().lstrip("/").strip().casefold()
    return unit[:50] or None


def _parse_instant(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_item(raw: dict[str, Any], *, search_term: str) -> NormalizedDeal | str:
    """One raw flyer item -> a :class:`NormalizedDeal`, or a skip reason string.

    Returning the reason rather than None keeps the run report honest about
    *why* coverage is lower than the hit count.
    """
    l1 = raw.get("_L1")
    if isinstance(l1, str) and l1.strip() and l1 not in FOOD_L1:
        return "non_food"

    name = raw.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name:
        # Seen live: 1/35 flyer items has name=None (image-only placement).
        return "no_name"

    sale_price = _number(raw.get("current_price"))
    if sale_price is None or sale_price <= 0:
        # Seen live: 2/35 have current_price=None. Some of those still carry an
        # original_price, but a flyer item with no advertised price is not an
        # offer we can cost a recipe from, and deals.sale_price is NOT NULL.
        return "no_price"

    regular_price = _number(raw.get("original_price"))
    if regular_price is not None and regular_price <= sale_price:
        # A "regular" price at or below the sale price is not a regular price.
        regular_price = None

    valid_from = _parse_instant(raw.get("valid_from"))
    valid_to = _parse_instant(raw.get("valid_to"))
    if valid_from is None or valid_to is None or valid_to < valid_from:
        return "bad_dates"

    merchant = raw.get("merchant_name")
    # Seen live: "Cataldi\t" — merchant names arrive with stray whitespace.
    merchant = merchant.strip() if isinstance(merchant, str) else ""
    if not merchant:
        return "no_merchant"

    item_id = raw.get("flyer_item_id") or raw.get("id")
    if item_id is None:
        return "no_item_id"

    merchant_id = raw.get("merchant_id")

    return NormalizedDeal(
        product_name=name[:255],
        merchant_name=merchant[:255],
        merchant_id=str(merchant_id) if merchant_id is not None else None,
        sale_price=round(sale_price, 2),
        regular_price=round(regular_price, 2) if regular_price is not None else None,
        unit=_clean_unit(raw.get("post_price_text")),
        category=category_for_term(search_term),
        valid_from=valid_from.date(),
        valid_until=valid_to.date(),
        source_item_id=str(item_id),
        search_term=search_term,
        sale_story=(raw.get("sale_story") or None),
    )


def normalize_items(
    raw_items: Iterable[dict[str, Any]],
    *,
    search_term: str,
    report: IngestionReport | None = None,
) -> list[NormalizedDeal]:
    """Normalise one term's results, tallying skips into ``report``."""
    out: list[NormalizedDeal] = []
    counters = {
        "no_name": "skipped_no_name",
        "no_price": "skipped_no_price",
        "non_food": "skipped_non_food",
        "bad_dates": "skipped_bad_dates",
        "no_merchant": "skipped_no_name",
        "no_item_id": "skipped_bad_dates",
    }
    for raw in raw_items:
        result = normalize_item(raw, search_term=search_term)
        if isinstance(result, str):
            if report is not None:
                attr = counters.get(result)
                if attr:
                    setattr(report, attr, getattr(report, attr) + 1)
            continue
        out.append(result)
    return out


def _sort_key(deal: NormalizedDeal) -> tuple[int, int | str]:
    """Deterministic ordering for picking a duplicate group's representative.

    Numeric ids sort numerically; anything non-numeric falls to the back and
    sorts as a string, so the choice never depends on provider result order.
    """
    try:
        return (0, int(deal.source_item_id))
    except (TypeError, ValueError):
        return (1, deal.source_item_id)


def dedupe(deals: Sequence[NormalizedDeal]) -> tuple[list[NormalizedDeal], int, int]:
    """Collapse items seen more than once in a single run.

    Returns ``(unique, same_item_dropped, same_offer_dropped)``.

    Two separate kinds of duplicate, both observed live:

    1. **Same provider item, found by two terms.** Overlapping staples
       genuinely collide — "chicken breast" and "chicken thighs" both return
       the same "CHICKEN DRUMSTICKS, THIGHS OR BREAST FILLET" item. First term
       wins, which also makes the derived category stable in term order.

    2. **Same offer, printed twice in one flyer.** A merchant can place the
       identical offer in two spots, and each placement gets its own
       ``flyer_item_id``. Observed on 2026-10-14 across 2 terms: Food Basics'
       "PRIME ... FRESH BONELESS" at $11.99/lb as items 1045444708 and
       1045444966, plus three more pairs.

       Collapsing (2) is not cosmetic. Left in, each placement becomes its own
       ``deals`` row *and* its own ``price_snapshots`` row, which double-weights
       that product-day in the trailing median
       (``app/services/price_baseline.py``) — one printed offer would count as
       two observations.

       The representative is chosen by lowest ``source_item_id``, NOT by
       encounter order. Encounter order follows provider relevance ranking,
       which is not stable between runs; picking a different placement next
       week would insert a second row under the natural key instead of
       updating the first, and the idempotency guarantee would quietly fail.
    """
    by_item: dict[tuple[str, str], NormalizedDeal] = {}
    same_item_dropped = 0
    for deal in deals:
        key = (deal.merchant_name.casefold(), deal.source_item_id)
        if key in by_item:
            same_item_dropped += 1
            continue
        by_item[key] = deal

    offers: dict[tuple[str, str, float, date], list[NormalizedDeal]] = {}
    order: list[tuple[str, str, float, date]] = []
    for deal in by_item.values():
        key = (
            deal.merchant_name.casefold(),
            deal.product_name.casefold(),
            deal.sale_price,
            deal.valid_from,
        )
        if key not in offers:
            offers[key] = []
            order.append(key)
        offers[key].append(deal)

    unique: list[NormalizedDeal] = []
    same_offer_dropped = 0
    for key in order:
        group = offers[key]
        same_offer_dropped += len(group) - 1
        unique.append(min(group, key=_sort_key))

    return unique, same_item_dropped, same_offer_dropped


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_STORE_INSERT = """
INSERT INTO stores (name, chain, postal_code, source, source_merchant_id, is_active)
VALUES (%(name)s, %(chain)s, %(postal_code)s, %(source)s, %(merchant_id)s, true)
ON CONFLICT (source, source_merchant_id, postal_code) DO NOTHING
RETURNING store_id;
"""

_STORE_SELECT = """
SELECT store_id FROM stores
WHERE source = %(source)s
  AND source_merchant_id = %(merchant_id)s
  AND postal_code = %(postal_code)s
LIMIT 1;
"""

# Natural key: (store, source, source item, week start). ``valid_from`` is in
# the key so next week's flyer is a new row rather than an overwrite of this
# week's history, while a re-run of the same week collapses onto itself.
_DEAL_UPSERT = """
INSERT INTO deals (
    store_id, product_name, brand, sale_price, regular_price,
    discount_percentage, unit, category, valid_from, valid_until,
    source, source_item_id
) VALUES (
    %(store_id)s, %(product_name)s, NULL, %(sale_price)s, %(regular_price)s,
    %(discount_percentage)s, %(unit)s, %(category)s, %(valid_from)s, %(valid_until)s,
    %(source)s, %(source_item_id)s
)
ON CONFLICT (store_id, source, source_item_id, valid_from) DO UPDATE SET
    product_name        = EXCLUDED.product_name,
    sale_price          = EXCLUDED.sale_price,
    regular_price       = EXCLUDED.regular_price,
    discount_percentage = EXCLUDED.discount_percentage,
    unit                = COALESCE(EXCLUDED.unit, deals.unit),
    -- first term to find an item owns its category, so a re-run in a
    -- different term order does not churn the value
    category            = COALESCE(deals.category, EXCLUDED.category),
    valid_until         = EXCLUDED.valid_until
-- (xmax = 0) distinguishes insert from update. It drives report counters only,
-- never control flow, because the idiom can misreport under a concurrent
-- writer -- and the refresh lock in app/routes/stores.py means there is not
-- normally one.
RETURNING deal_id, (xmax = 0) AS was_inserted;
"""

# One observation per item per *day*: ``time`` is truncated to UTC midnight so
# two runs on the same day do not double-weight that day in the median
# baseline. Flyers are weekly, so daily granularity is already generous.
_SNAPSHOT_INSERT = """
INSERT INTO price_snapshots (
    time, store_id, product_name, brand, price, sale_price,
    unit, category, source, source_item_id, price_basis
) VALUES (
    %(observed_on)s, %(store_id)s, %(product_name)s, NULL, %(price)s, %(sale_price)s,
    %(unit)s, %(category)s, %(source)s, %(source_item_id)s, %(price_basis)s
)
ON CONFLICT (store_id, source, source_item_id, time) DO NOTHING
RETURNING snapshot_id;
"""


def _observation_day(now: datetime | None = None) -> datetime:
    """The run's snapshot timestamp: today at UTC midnight."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def persist_deals(
    deals: Sequence[NormalizedDeal],
    *,
    postal_code: str,
    source: str,
    report: IngestionReport,
    observed_on: datetime | None = None,
    database=db,
) -> IngestionReport:
    """Write normalised deals to ``deals`` and ``price_snapshots``.

    Idempotent on both tables (see the SQL above). Runs in one transaction:
    either the whole refresh lands or none of it does, so a half-written week
    never reaches recipe generation.
    """
    observed_on = observed_on or _observation_day()
    report.observed_on = observed_on
    store_ids: dict[tuple[str, str | None], int] = {}

    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            for deal in deals:
                store_key = (deal.merchant_name.casefold(), deal.merchant_id)
                store_id = store_ids.get(store_key)
                if store_id is None:
                    store_id = _upsert_store(cursor, deal, postal_code, source, report)
                    store_ids[store_key] = store_id

                cursor.execute(
                    _DEAL_UPSERT,
                    {
                        "store_id": store_id,
                        "product_name": deal.product_name,
                        "sale_price": deal.sale_price,
                        "regular_price": deal.regular_price,
                        "discount_percentage": deal.discount_percentage,
                        "unit": deal.unit,
                        "category": deal.category,
                        "valid_from": deal.valid_from,
                        "valid_until": deal.valid_until,
                        "source": source,
                        "source_item_id": deal.source_item_id,
                    },
                )
                row = cursor.fetchone()
                if row and row["was_inserted"]:
                    report.deals_inserted += 1
                else:
                    report.deals_updated += 1

                cursor.execute(
                    _SNAPSHOT_INSERT,
                    {
                        "observed_on": observed_on,
                        "store_id": store_id,
                        "product_name": deal.product_name,
                        # price = shelf/regular, sale_price = promo. When the
                        # source gave us only one number it is the promo one,
                        # so price stays NULL and price_basis says so.
                        "price": deal.regular_price,
                        "sale_price": deal.sale_price,
                        "unit": deal.unit,
                        "category": deal.category,
                        "source": source,
                        "source_item_id": deal.source_item_id,
                        "price_basis": deal.price_basis,
                    },
                )
                if cursor.fetchone():
                    report.snapshots_inserted += 1
                else:
                    report.snapshots_skipped += 1

    return report


def _upsert_store(cursor, deal: NormalizedDeal, postal_code: str, source: str,
                  report: IngestionReport) -> int:
    """Find-or-create the merchant's store row for this postal code.

    INSERT .. DO NOTHING then SELECT, rather than DO UPDATE, on purpose: the
    origin schema's ``update_stores_updated_at`` trigger calls
    ``update_updated_at_column()``, which assigns ``NEW.updated_at`` — a column
    ``stores`` does not have (it has ``last_updated``). Any UPDATE on ``stores``
    therefore raises. ``migrations/004`` fixes the trigger; this path avoids
    depending on it.
    """
    key = {
        "postal_code": postal_code,
        "source": source,
        "merchant_id": deal.merchant_id,
    }
    cursor.execute(
        _STORE_INSERT,
        {**key, "name": deal.merchant_name, "chain": deal.merchant_name[:100]},
    )
    row = cursor.fetchone()
    if row:
        report.stores_created += 1
        return row["store_id"]

    cursor.execute(_STORE_SELECT, key)
    row = cursor.fetchone()
    if not row:
        raise RuntimeError(
            f"store upsert for {deal.merchant_name!r} in {postal_code} neither "
            "inserted nor matched — check migrations/001 is applied"
        )
    return row["store_id"]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def collect_deals(
    provider,
    postal_code: str,
    *,
    terms: Sequence[str] | None = None,
    report: IngestionReport | None = None,
) -> tuple[list[NormalizedDeal], IngestionReport]:
    """Search every term and return the deduped, normalised deals.

    Network only — no DB. A term that fails after the client's retries is
    recorded in ``report.terms_failed`` and the run continues: one bad term
    should not cost a whole refresh.
    """
    terms = list(terms) if terms is not None else staple_terms()
    report = report or IngestionReport(postal_code=postal_code, source=provider.source)

    collected: list[NormalizedDeal] = []
    for term in terms:
        report.terms_searched += 1
        try:
            raw_items = provider.search(term, postal_code)
        except (DealProviderError, ValueError) as exc:
            logger.warning("Ingestion: term %r failed: %s", term, exc)
            report.terms_failed.append(term)
            continue
        report.raw_items += len(raw_items)
        collected.extend(normalize_items(raw_items, search_term=term, report=report))

    unique, same_item, same_offer = dedupe(collected)
    report.duplicate_items = same_item
    report.duplicate_offers = same_offer
    report.normalized = len(unique)
    report.sale_price_only = sum(1 for d in unique if d.regular_price is None)
    return unique, report


def run_ingestion(
    postal_code: str,
    *,
    provider=None,
    terms: Sequence[str] | None = None,
    observed_on: datetime | None = None,
    database=db,
) -> IngestionReport:
    """Full refresh for one postal code: search, normalise, persist.

    Callers are responsible for checking ``settings.deal_ingestion_enabled``
    first — see ``app/routes/stores.py`` and ``scripts/ingest_deals.py``.
    """
    if provider is None:  # pragma: no cover - needs network
        from app.services.flipp_client import FlippSearchClient

        provider = FlippSearchClient()

    report = IngestionReport(postal_code=postal_code, source=provider.source)
    deals, report = collect_deals(provider, postal_code, terms=terms, report=report)

    if not deals:
        logger.warning(
            "Ingestion for %s produced no usable deals (%d raw items, %d terms failed)",
            postal_code, report.raw_items, len(report.terms_failed),
        )
        report.observed_on = observed_on or _observation_day()
        return report

    persist_deals(
        deals,
        postal_code=postal_code,
        source=report.source,
        report=report,
        observed_on=observed_on,
        database=database,
    )
    logger.info("Ingestion for %s: %s", postal_code, report.as_dict())
    return report
