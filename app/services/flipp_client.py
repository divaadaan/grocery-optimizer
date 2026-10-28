"""HTTP client for the Flipp flyer-search backend.

UNSANCTIONED ENDPOINT — READ THIS BEFORE ENABLING
=================================================
This talks to ``https://backflipp.wishabi.com/flipp/items/search``, the
keyless JSON backend behind flipp.com's own search box. It is **not** a
documented, supported or licensed API:

* there is no published rate limit, SLA, or terms-of-use grant for it;
* it can change shape or disappear without notice;
* Flipp would be entirely within its rights to block us.

Why this and not the "real" API: there is no real API. ``FLIPP_API_KEY`` in
``.env`` was never anything but the ``.env.example`` placeholder, and
``https://api.flipp.com/v2`` (the old ``flipp_api_url`` default) does not
correspond to any documented product. Both settings have been retired — see
``app/config.py``.

Consequences baked into this module:

* **Off by default.** ``settings.deal_ingestion_enabled`` is False unless
  someone sets ``DEAL_INGESTION_ENABLED=true``. Nothing here makes a network
  call until it is on.
* **Polite by construction.** Identifying User-Agent, hard timeout, bounded
  retries with exponential backoff on transport errors and 5xx/429 only, and
  a minimum interval between requests enforced *inside* the client so no
  caller can accidentally hammer it.
* **One request per staple term.** See ``app/services/flipp_staples.py``.

Tests never touch the network: ``DealProvider`` is the seam, and
``app/tests/test_flipp_ingestion.py`` drives a fake built from the recorded
fixture ``app/tests/fixtures/flipp_search_chicken_breast.json``.

Observed response shape (live probe, postal_code=M5V3A8, 2026-10-14)
--------------------------------------------------------------------
Top level is an object with these keys, all always present::

    ads, coupons, coupons_v2, ecom_items, facets, flyers, items,
    merchants, native_ads, normalized_query, outside_fsa_flyers,
    related_flyers, related_items, sort

``items`` holds the flyer items — the only ones we ingest, because they are
the ones with a validity window. Fields seen on a flyer item, with observed
presence out of 35 results for "chicken breast"::

    name              str   34/35   product name, as printed on the flyer
    current_price     num   33/35   the advertised price (int OR float)
    original_price    num    5/35   pre-promo price; USUALLY ABSENT
    post_price_text   str   23/35   unit suffix: "lb", "/lb", "LB", "/100 g"
    pre_price_text    str    3/35   "NOW", "from", "Online Price"
    sale_story        str   13/35   free text: "save $10", "SAVE 30%", "Rollback"
    merchant_name     str   35/35   e.g. "No Frills"; seen with a trailing tab
    merchant_id       int   35/35
    flyer_id          int   35/35
    flyer_item_id     int   35/35   stable per flyer item -> our natural key
    valid_from        str   35/35   ISO 8601 with offset, e.g. 2026-10-08T04:00:00+00:00
    valid_to          str   35/35   ISO 8601 with offset
    _L1, _L2          str   35/35   Google product taxonomy; useless at this
                                    granularity (every grocery item is
                                    "Food, Beverages & Tobacco"/"Food Items")
    id, item_type, premium, indexed, score, item_weight,
    top/left/right/bottom, clean_image_url, clipping_image_url,
    merchant_logo, brand_ids               -- layout/plumbing, not ingested

``ecom_items`` is a *different* shape (``merchant`` not ``merchant_name``,
``sku``/``item_id``, no ``valid_from``/``valid_to``) and is online-only
inventory rather than a flyer promotion, so it is deliberately not ingested.

The two headline facts for the product: ``original_price`` is present on only
~14% of results, and ``items`` carries no store address — just a merchant. See
``app/services/flipp_ingestion.py`` for how both are handled.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Protocol

import requests

from app.config import settings

logger = logging.getLogger(__name__)

# Status codes worth another attempt. Anything else (403, 404, 400) is a
# permanent answer and retrying it is just rudeness.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class DealProviderError(RuntimeError):
    """A search could not be completed after the configured retries."""


class DealProvider(Protocol):
    """The ingestion seam.

    Anything that can answer "what is on sale for <term> near <postal_code>"
    with a list of raw provider dicts can drive ingestion. Implemented by
    :class:`FlippSearchClient` in production and by a fixture-backed fake in
    the tests.
    """

    #: Provenance tag written to ``deals.source`` / ``price_snapshots.source``.
    source: str

    def search(self, term: str, postal_code: str) -> list[dict[str, Any]]:
        """Raw flyer-item dicts for one term, or [] if the term has no hits."""
        ...


class FlippSearchClient:
    """Rate-limited, retrying, keyless client for the Flipp search backend."""

    source = "flipp"

    def __init__(
        self,
        *,
        url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        min_interval: float | None = None,
        user_agent: str | None = None,
        session: requests.Session | None = None,
        sleep=time.sleep,
    ) -> None:
        self.url = url or settings.flipp_search_url
        self.timeout = timeout if timeout is not None else settings.flipp_timeout_seconds
        self.max_retries = max_retries if max_retries is not None else settings.flipp_max_retries
        self.min_interval = (
            min_interval if min_interval is not None else settings.flipp_min_request_interval
        )
        self.user_agent = user_agent or settings.flipp_user_agent
        self._session = session or requests.Session()
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_request_at = 0.0
        # Responses that had no 'items' list. search() returns [] for those, the
        # same as a term with no hits, so the count is the only trace; ingestion
        # reads it (collect_deals) to flag a payload-shape change.
        self.missing_items_responses = 0

    # -- politeness ---------------------------------------------------------

    def _throttle(self) -> None:
        """Block until ``min_interval`` has elapsed since the last request.

        Held under a lock so concurrent ingestion of two postal codes still
        respects a single global request interval.
        """
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                self._sleep(wait)
            self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter: ~0.5s, 1s, 2s, 4s (capped 30s)."""
        base = min(0.5 * (2 ** attempt), 30.0)
        return base * (0.5 + random.random() / 2)

    # -- request ------------------------------------------------------------

    def search(self, term: str, postal_code: str) -> list[dict[str, Any]]:
        """Fetch flyer items for one search term.

        An empty ``term`` is refused rather than sent: the endpoint answers an
        empty ``q`` with the entire national corpus (150 items of mattresses,
        tools and diapers on probe), which is both useless and abusive.
        """
        term = (term or "").strip()
        if not term:
            raise ValueError("search term must not be empty")

        payload = self._get(
            params={"q": term, "postal_code": postal_code},
        )
        items = payload.get("items")
        if not isinstance(items, list):
            self.missing_items_responses += 1
            logger.warning(
                "Flipp response for %r had no 'items' list (keys=%s)",
                term,
                sorted(payload) if isinstance(payload, dict) else type(payload).__name__,
            )
            return []
        return [item for item in items if isinstance(item, dict)]

    def _get(self, params: dict[str, str]) -> dict[str, Any]:
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self._session.get(
                    self.url,
                    params=params,
                    timeout=self.timeout,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept": "application/json",
                    },
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "Flipp request failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
            else:
                if response.status_code in RETRYABLE_STATUS:
                    last_error = DealProviderError(
                        f"Flipp returned HTTP {response.status_code}"
                    )
                    logger.warning(
                        "Flipp returned retryable HTTP %d (attempt %d/%d)",
                        response.status_code,
                        attempt + 1,
                        self.max_retries + 1,
                    )
                elif not response.ok:
                    # Permanent: do not retry, do not pretend it worked.
                    raise DealProviderError(
                        f"Flipp returned HTTP {response.status_code} for {params!r}"
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise DealProviderError(
                            f"Flipp returned non-JSON body for {params!r}"
                        ) from exc

            if attempt < self.max_retries:
                self._sleep(self._backoff(attempt))

        raise DealProviderError(
            f"Flipp search for {params!r} failed after "
            f"{self.max_retries + 1} attempt(s)"
        ) from last_error
