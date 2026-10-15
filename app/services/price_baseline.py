"""Self-derived price baseline from our own ``price_snapshots`` history.

Why this exists
---------------
``deals.regular_price`` is nullable and the flyer source populates it on only
~14% of items, so "savings = regular - sale" is not a metric we can rely on.
The alternative is to stop trusting the source and measure instead: every
ingestion run appends a snapshot (``app/services/flipp_ingestion.py``), and
after enough weeks those snapshots support a trailing median we derived
ourselves.

TWO LIMITS THAT MUST NOT BE PAPERED OVER
----------------------------------------
**(a) There is no baseline on day 1, and there must not be a fake one.**
A median of one observation is that observation; a median of two is their
mean. Either would make today's advertised price look like the historical
norm and report ~$0 of savings forever. So :func:`baseline_from_rows`
returns ``None`` until it has observations from at least
``MIN_DISTINCT_WEEKS`` *distinct* weeks. Flyers turn over weekly, which means
one usable sample per week per item — so a baseline is roughly **4 weeks
away at the earliest and 8 weeks away before it is worth much**, counted from
the first ingestion run, not from today. Callers must handle ``None`` as
"unknown", never as zero.

**(b) This measures advertised prices, not shelf prices.**
Our snapshots come from flyers. A flyer only prints an item when it is *on
promotion*, so the population we sample is sale prices, with a shelf price
attached on the minority of rows where the source bothered to send
``original_price``. The median of that population is the item's **typical
advertised price**, which sits *below* its true shelf price. Any "savings"
computed against it is therefore an understatement — it answers "is this week
cheaper than this item usually goes on sale for?", not "how much am I saving
off the regular price?".

That is exactly why nothing in this module is called ``regular_price`` or
``estimated_savings``. The metric is ``observed_sale_median`` and the delta is
``discount_vs_typical_sale``. Renaming these to anything that implies a shelf
price would re-introduce the dishonesty this module exists to avoid. Closing
the gap needs a non-flyer price source (store shelf prices), not more flyer
history.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from app.db import db

logger = logging.getLogger(__name__)

#: Trailing window, in days. 8 weeks of weekly flyers ~= 8 samples per item,
#: which is enough for a median to stop lurching on a single loss-leader week
#: without reaching so far back that it tracks last season's prices.
DEFAULT_WINDOW_DAYS = 56

#: Distinct ISO weeks of observations required before a baseline is returned.
#: Below this the "median" is just a recent price wearing a statistics hat.
MIN_DISTINCT_WEEKS = 4


@dataclass(frozen=True)
class SaleBaseline:
    """What this item has *typically been advertised at*, from our own history.

    Attributes:
        observed_sale_median: median advertised price over the window. NOT a
            shelf price — see the module docstring, limit (b).
        sample_count: snapshot rows behind the median.
        distinct_weeks: distinct ISO weeks those rows came from. This, not
            ``sample_count``, is what gates the baseline, because one weekly
            flyer re-ingested daily is one week of information.
        shelf_price_samples: rows that also carried a real pre-promo price
            (``price_basis='regular_and_sale'``). When this is 0 — the usual
            case — the median is purely a sale-price median.
    """

    product_name: str
    observed_sale_median: float
    sample_count: int
    distinct_weeks: int
    window_days: int
    first_observed: date
    last_observed: date
    shelf_price_samples: int

    @property
    def measures_sale_prices_only(self) -> bool:
        """True when no observation carried a shelf price — read limit (b)."""
        return self.shelf_price_samples == 0


def _iso_week(value: Any) -> tuple[int, int] | None:
    if isinstance(value, date):
        iso = value.isocalendar()
        return (iso[0], iso[1])
    return None


def baseline_from_rows(
    rows: Iterable[dict[str, Any]],
    *,
    product_name: str,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_distinct_weeks: int = MIN_DISTINCT_WEEKS,
) -> SaleBaseline | None:
    """Compute a baseline from snapshot rows, or None if the history is too thin.

    Pure: the DB-free half of :func:`sale_baseline`, so the day-1 behaviour is
    unit-testable. Each row needs ``observed_on`` (a ``date``), ``sale_price``
    and ``price_basis``.

    Returns ``None`` — never a placeholder number — when fewer than
    ``min_distinct_weeks`` distinct weeks are represented.
    """
    prices: list[float] = []
    weeks: set[tuple[int, int]] = set()
    days: list[date] = []
    shelf_samples = 0

    for row in rows:
        price = row.get("sale_price")
        if price is None:
            continue
        observed_on = row.get("observed_on")
        week = _iso_week(observed_on)
        if week is None:
            continue
        prices.append(float(price))
        weeks.add(week)
        days.append(observed_on)
        if row.get("price_basis") == "regular_and_sale":
            shelf_samples += 1

    if not prices:
        return None

    if len(weeks) < min_distinct_weeks:
        # Day 1 (and week 1, 2, 3) land here. Returning None is the whole
        # point: a caller that wants a number must be told we do not have one.
        logger.debug(
            "No baseline for %r: %d distinct week(s) of history, need %d",
            product_name, len(weeks), min_distinct_weeks,
        )
        return None

    return SaleBaseline(
        product_name=product_name,
        observed_sale_median=round(statistics.median(prices), 2),
        sample_count=len(prices),
        distinct_weeks=len(weeks),
        window_days=window_days,
        first_observed=min(days),
        last_observed=max(days),
        shelf_price_samples=shelf_samples,
    )


_SNAPSHOT_HISTORY = """
SELECT
    (time AT TIME ZONE 'UTC')::date AS observed_on,
    sale_price,
    price,
    price_basis
FROM price_snapshots
WHERE product_name = %(product_name)s
  AND time >= NOW() - (%(window_days)s * INTERVAL '1 day')
  AND sale_price IS NOT NULL
"""

_SNAPSHOT_HISTORY_STORE = " AND store_id = %(store_id)s"
_SNAPSHOT_HISTORY_ORDER = " ORDER BY time DESC;"


def sale_baseline(
    product_name: str,
    *,
    store_id: int | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
    min_distinct_weeks: int = MIN_DISTINCT_WEEKS,
    database=db,
) -> SaleBaseline | None:
    """Trailing median advertised price for one product, or None if unknown.

    Matches ``product_name`` exactly — ingestion stores the raw flyer string,
    so "CHICKEN BREAST" and "Fresh Chicken Breast" are separate series. That
    is honest but sparse; collapsing them is what ``price_snapshots.product_id``
    (linked by ``app/services/products.py``) is for, but this lookup does not
    use it yet. ``idx_price_snapshots_product_trgm`` is
    already in place for whoever builds the fuzzy version.
    """
    if not product_name:
        return None

    query = _SNAPSHOT_HISTORY
    params: dict[str, Any] = {"product_name": product_name, "window_days": window_days}
    if store_id is not None:
        query += _SNAPSHOT_HISTORY_STORE
        params["store_id"] = store_id
    query += _SNAPSHOT_HISTORY_ORDER

    with database.get_cursor() as cursor:
        cursor.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]

    return baseline_from_rows(
        rows,
        product_name=product_name,
        window_days=window_days,
        min_distinct_weeks=min_distinct_weeks,
    )


def discount_vs_typical_sale(
    sale_price: float,
    baseline: SaleBaseline | None,
) -> float | None:
    """How much below its typical advertised price this offer is, or None.

    NOT savings off a regular price — see the module docstring, limit (b).
    Negative results are returned as-is: an offer priced *above* the item's
    typical sale price is real information, and clamping it to 0 would hide it.
    """
    if baseline is None:
        return None
    return round(baseline.observed_sale_median - float(sale_price), 2)
