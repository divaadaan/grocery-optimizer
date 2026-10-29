"""
Store and postal code discovery endpoints
"""

from fastapi import APIRouter, HTTPException, status, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
import logging
import threading
import time

from app.models.schemas import (
    PostalCodeDiscoveryRequest,
    PostalCodeDiscoveryResponse,
    StoreInfo,
    DealInfo,
    ErrorResponse
)
from app.config import settings
from app.services.store_service import StoreService
from app.services.flipp_ingestion import run_ingestion
from app.services.flipp_staples import STAPLES, staple_terms

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/postal-code", tags=["stores"])

# One refresh at a time, process-wide. A refresh is one HTTP request per staple
# term against an endpoint that owes us nothing (see app/services/flipp_client.py),
# so two concurrent refreshes would double the request rate against it for no
# benefit — the second would just overwrite the first's rows with identical
# values. try-acquire rather than queueing: a caller who asked for a refresh
# that is already running wants to be told so, not parked for a minute.
#
# threading.Lock, not asyncio.Lock: the work itself runs in the threadpool, and
# an asyncio.Lock binds to the first event loop that awaits it and then raises
# if it is ever used from another one. A non-blocking acquire is also atomic,
# which a `locked()` check followed by an acquire is not.
_refresh_lock = threading.Lock()

# Cold-start auto-ingest backstop. GET-shaped traffic must never be able to
# drive a provider crawl per call: a postal code with genuinely no flyer
# coverage returns zero deals forever, so "ingest when the DB is empty" would
# re-crawl on every single discover call. One auto-attempt per postal code per
# AUTO_INGEST_COOLDOWN. Explicit ?refresh=true and POST /refresh bypass it —
# those are a human asking.
AUTO_INGEST_COOLDOWN_SECONDS = 3600.0
_last_auto_ingest: Dict[str, float] = {}


def _auto_ingest_due(postal_code: str) -> bool:
    last = _last_auto_ingest.get(postal_code)
    return last is None or (time.monotonic() - last) >= AUTO_INGEST_COOLDOWN_SECONDS


# There is no worker infrastructure and no scheduler in this project, so a
# refresh is a manually triggered, synchronous-to-the-caller operation that runs
# in the threadpool. At ~1.5s minimum spacing x the staple list that is a
# multi-minute request; clients should call it deliberately, not on page load.
INGESTION_DISABLED_DETAIL = (
    "Deal ingestion is disabled. It talks to an unsanctioned, keyless Flipp "
    "endpoint, so it is off by default: set DEAL_INGESTION_ENABLED=true to "
    "enable it, and apply migrations/001-003 first. See "
    "app/services/flipp_client.py."
)


class DealRefreshResponse(BaseModel):
    """Outcome of one ingestion run.

    Defined here rather than in app/models/schemas.py to keep this change off a
    file another workstream is editing.
    """

    postal_code: str
    source: str
    enabled: bool
    terms_searched: int
    terms_failed: List[str]
    deals_inserted: int
    deals_updated: int
    snapshots_inserted: int
    stores_created: int
    # Fraction of ingested deals that arrived with a pre-promo price. Observed
    # ~0.14 live, which is why savings is a secondary metric and the
    # self-derived baseline (app/services/price_baseline.py) exists.
    regular_price_coverage: float
    report: Dict[str, Any] = Field(
        default_factory=dict, description="Full IngestionReport, including skip counts"
    )
    message: str


class StaplesResponse(BaseModel):
    """The search terms that define ingestion coverage."""

    count: int
    terms_by_category: Dict[str, List[str]]
    note: str


async def _run_refresh(postal_code: str, terms: Optional[List[str]] = None,
                       trigger: str = "refresh_route"):
    """Acquire the refresh lock and run ingestion in the threadpool.

    ``trigger`` is recorded on the ``ingestion_runs`` row: ``refresh_route`` for
    a human asking, ``cold_start`` for discover's auto-ingest.
    """
    if not _refresh_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A deal refresh is already running. Try again when it finishes.",
        )
    try:
        return await run_in_threadpool(
            run_ingestion, postal_code, terms=terms, trigger=trigger
        )
    finally:
        _refresh_lock.release()


@router.post("/discover",
             response_model=PostalCodeDiscoveryResponse,
             responses={
                 404: {"model": ErrorResponse, "description": "No stores found"},
                 500: {"model": ErrorResponse, "description": "Internal server error"}
             })
async def discover_postal_code(
    request: PostalCodeDiscoveryRequest,
    refresh: bool = Query(
        False,
        description="Fetch fresh deals from the provider before reading. Slow "
                    "(one request per staple term); no-op unless "
                    "DEAL_INGESTION_ENABLED is set.",
    ),
) -> PostalCodeDiscoveryResponse:
    """
    Discover stores and deals for a postal code.

    Fetches all available stores and current deals for the specified postal code.
    This is the first step before generating meal plans.

    - **postal_code**: Canadian postal code (e.g., "M5V3A8")
    - **refresh**: force a provider fetch first (see `POST /postal-code/refresh`)

    Previously this only ever read what was already in the DB. It now *can*
    populate it: when ingestion is enabled it runs a refresh if asked
    explicitly, or automatically when the postal code has no active deals at
    all (a genuinely cold start). It deliberately does NOT refresh on every
    call — that would mean a provider crawl per page load.

    Returns store count, deal count, and list of stores found.
    """
    try:
        postal_code = request.postal_code
        ingestion_note = ""

        stores = StoreService.get_stores_by_postal_code(postal_code)
        deals = StoreService.get_current_deals_by_postal_code(postal_code)

        cold_start = not deals and _auto_ingest_due(postal_code)
        should_ingest = settings.deal_ingestion_enabled and (refresh or cold_start)

        if should_ingest and _refresh_lock.locked():
            # A refresh is already in flight. Discover is a read; serve what is
            # in the DB rather than 409-ing the caller or queueing behind a
            # multi-minute crawl.
            should_ingest = False
            ingestion_note = " (a deal refresh is already running; showing current data)"

        if should_ingest:
            logger.info(
                "Discover: triggering ingestion for %s (refresh=%s, deals_in_db=%d)",
                postal_code, refresh, len(deals),
            )
            if not refresh:
                _last_auto_ingest[postal_code] = time.monotonic()
            try:
                report = await _run_refresh(
                    postal_code,
                    trigger="refresh_route" if refresh else "cold_start",
                )
            except HTTPException as exc:
                # Lost the race for the lock between the check above and the
                # acquire. Discover is a read: degrade to current data rather
                # than failing the caller's request.
                if exc.status_code != status.HTTP_409_CONFLICT:
                    raise
                ingestion_note = (
                    " (a deal refresh is already running; showing current data)"
                )
            else:
                ingestion_note = (
                    f" Refreshed from {report.source}: "
                    f"{report.deals_inserted} new deal(s), "
                    f"{report.deals_updated} updated, "
                    f"{report.snapshots_inserted} price snapshot(s)."
                )
                # Re-read after ingestion. get_current_deals_by_postal_code
                # caches, so the stale pre-ingestion entry must be stepped over.
                StoreService.invalidate_deal_cache(postal_code)
                stores = StoreService.get_stores_by_postal_code(postal_code)
                deals = StoreService.get_current_deals_by_postal_code(postal_code)
        elif refresh and not settings.deal_ingestion_enabled:
            ingestion_note = " (refresh ignored: DEAL_INGESTION_ENABLED is off)"

        if not stores:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No stores found for postal code {postal_code}"
            )

        # Convert stores to response format
        store_infos = [
            StoreInfo(
                store_id=store["store_id"],
                name=store["name"],
                chain=store["chain"],
                postal_code=store["postal_code"],
                address=store["address"],
                city=store["city"],
                province=store["province"]
            )
            for store in stores
        ]

        logger.info(f"Found {len(stores)} stores and {len(deals)} deals for {postal_code}")

        return PostalCodeDiscoveryResponse(
            postal_code=postal_code,
            stores_found=len(stores),
            deals_count=len(deals),
            stores=store_infos,
            job_id=None,  # Would be populated if using background jobs
            message=(
                f"Found {len(stores)} stores with {len(deals)} active deals"
                f"{ingestion_note}"
            )
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error discovering postal code {request.postal_code}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to discover postal code"
        )


@router.post("/refresh",
             response_model=DealRefreshResponse,
             responses={
                 409: {"model": ErrorResponse, "description": "A refresh is already running"},
                 503: {"model": ErrorResponse, "description": "Ingestion is disabled"},
             })
async def refresh_deals(
    request: PostalCodeDiscoveryRequest,
    terms: Optional[List[str]] = Query(
        None,
        description="Override the staple list for this run (ad-hoc terms are "
                    "allowed; their deals get no category).",
    ),
) -> DealRefreshResponse:
    """
    Pull fresh deals from the provider for a postal code.

    This is the manual trigger. **There is no scheduler** — nothing in this
    project refreshes deals on a timer, by design: there is no worker
    infrastructure, and the provider is an unsanctioned endpoint that should be
    polled deliberately rather than on a cron nobody is watching. Call this (or
    `scripts/ingest_deals.py`) once a week, when flyers turn over.

    Coverage is exactly the staple terms in `app/services/flipp_staples.py`
    (see `GET /postal-code/staples`), because the provider is search-by-term
    only — a flyer cannot be enumerated.

    Re-running for the same postal code and flyer week is idempotent: deals
    upsert on a natural key and snapshots are one row per item per day.

    Returns 503 while `DEAL_INGESTION_ENABLED` is off.
    """
    if not settings.deal_ingestion_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=INGESTION_DISABLED_DETAIL,
        )

    postal_code = request.postal_code
    try:
        report = await _run_refresh(postal_code, terms=terms)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Deal refresh failed for {postal_code}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Deal refresh failed: {e}",
        )

    StoreService.invalidate_deal_cache(postal_code)

    message = (
        f"Ingested {report.deals_inserted} new and {report.deals_updated} "
        f"existing deal(s) from {report.source} across {report.terms_searched} "
        f"term(s); {report.snapshots_inserted} price snapshot(s) appended."
    )
    if report.terms_failed:
        message += f" {len(report.terms_failed)} term(s) failed."
    if report.status != "ok":
        message += f" Run flagged {report.status}: {'; '.join(report.health_reasons)}."
    if report.normalized and report.regular_price_coverage < 0.5:
        message += (
            f" Only {report.regular_price_coverage:.0%} of deals carry a regular "
            "price, so estimated_savings is unreliable for this batch."
        )

    return DealRefreshResponse(
        postal_code=postal_code,
        source=report.source,
        enabled=True,
        terms_searched=report.terms_searched,
        terms_failed=report.terms_failed,
        deals_inserted=report.deals_inserted,
        deals_updated=report.deals_updated,
        snapshots_inserted=report.snapshots_inserted,
        stores_created=report.stores_created,
        regular_price_coverage=report.regular_price_coverage,
        report=report.as_dict(),
        message=message,
    )


@router.get("/staples", response_model=StaplesResponse)
async def get_staples() -> StaplesResponse:
    """
    The staple search terms that define ingestion coverage.

    Edit `app/services/flipp_staples.py` to change them. Exposed read-only
    here so it is obvious from the API what ingestion does and does not cover.
    """
    by_category: Dict[str, List[str]] = {}
    for staple in STAPLES:
        by_category.setdefault(staple.category, []).append(staple.term)

    return StaplesResponse(
        count=len(staple_terms()),
        terms_by_category=by_category,
        note=(
            "Ingestion coverage is exactly these terms: the provider is "
            "search-by-term only, so a flyer cannot be enumerated. Categories "
            "are derived from the term that found an item, not from the "
            "provider payload (whose taxonomy labels every grocery item "
            "'Food Items'). Edit app/services/flipp_staples.py."
        ),
    )


@router.get("/deals/{postal_code}",
            response_model=List[DealInfo],
            responses={
                404: {"model": ErrorResponse, "description": "No deals found"}
            })
async def get_deals(
    postal_code: str,
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: Optional[int] = Query(50, ge=1, le=200, description="Maximum number of deals to return")
) -> List[DealInfo]:
    """
    Get current deals for a postal code.

    Fetch active deals with optional category filtering.

    - **postal_code**: Postal code to search
    - **category**: Optional category filter (e.g., "Produce", "Meat & Poultry")
    - **limit**: Maximum number of results (default: 50, max: 200)
    """
    try:
        deals = StoreService.get_current_deals_by_postal_code(postal_code, category)

        if not deals:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No deals found for postal code {postal_code}"
            )

        # Limit results
        deals = deals[:limit]

        deal_infos = [
            DealInfo(
                deal_id=deal["deal_id"],
                product_name=deal["product_name"],
                brand=deal["brand"],
                sale_price=deal["sale_price"],
                regular_price=deal["regular_price"],
                discount_percentage=deal["discount_percentage"],
                unit=deal["unit"],
                category=deal["category"],
                valid_from=deal["valid_from"],
                valid_until=deal["valid_until"],
                store_name=deal["store_name"],
                chain=deal["chain"]
            )
            for deal in deals
        ]

        return deal_infos

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching deals for {postal_code}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch deals"
        )


@router.get("/top-deals/{postal_code}",
            response_model=List[DealInfo])
async def get_top_deals(
    postal_code: str,
    limit: int = Query(20, ge=1, le=50, description="Number of top deals")
) -> List[DealInfo]:
    """
    Get top deals by discount percentage.

    Returns the best deals sorted by discount percentage.
    """
    deals = StoreService.get_top_deals(postal_code, limit)

    deal_infos = [
        DealInfo(
            deal_id=deal.get("deal_id", 0),
            product_name=deal["product_name"],
            brand=deal["brand"],
            sale_price=deal["sale_price"],
            regular_price=deal["regular_price"],
            discount_percentage=deal["discount_percentage"],
            unit=deal["unit"],
            category=deal["category"],
            valid_from=deal.get("valid_from"),
            valid_until=deal.get("valid_until"),
            store_name=deal["store_name"],
            chain=deal["chain"]
        )
        for deal in deals
    ]

    return deal_infos


@router.get("/search/{postal_code}",
            response_model=List[DealInfo])
async def search_deals(
    postal_code: str,
    q: str = Query(..., min_length=2, description="Search term (product name or brand)")
) -> List[DealInfo]:
    """
    Search deals by product name or brand.

    - **postal_code**: Postal code to search in
    - **q**: Search query (minimum 2 characters)
    """
    deals = StoreService.search_deals(postal_code, q)

    if not deals:
        return []

    deal_infos = [
        DealInfo(
            deal_id=deal["deal_id"],
            product_name=deal["product_name"],
            brand=deal["brand"],
            sale_price=deal["sale_price"],
            regular_price=deal["regular_price"],
            discount_percentage=deal["discount_percentage"],
            unit=deal["unit"],
            category=deal["category"],
            valid_from=deal.get("valid_from"),
            valid_until=deal.get("valid_until"),
            store_name=deal["store_name"],
            chain=deal["chain"]
        )
        for deal in deals
    ]

    return deal_infos
