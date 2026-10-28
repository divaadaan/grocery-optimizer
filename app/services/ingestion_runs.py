"""Ingestion run history and health: what ``IngestionReport`` was always missing.

An :class:`~app.services.flipp_ingestion.IngestionReport` used to be logged and
dropped. This module keeps one ``ingestion_runs`` row per real ingestion
(``migrations/020``), decides whether the run was healthy, and answers the two
freshness questions the rest of the system cannot: *when did we last get good
data* and *are there live deals right now*.

Three rules shape it:

* **Recording never fails the ingestion.** ``record_run`` opens its own
  connection (a separate transaction from the ingestion's), and swallows and
  logs any error, including the table not existing yet.
* **Dry runs are not recorded.** A dry run writes nothing by contract (it does
  not even touch the DB), and a row for it would pollute "last successful run"
  with data that was never ingested. The ``dry_run`` column exists so that can
  change without a migration; ``latest_successful_run`` already excludes it.
* **Health is a pure function of the report.** ``assess_health`` takes the
  report and thresholds and returns a verdict, so it is unit-testable and the
  CLI, the route and the table all agree.

The report is duck-typed here (no import of ``flipp_ingestion``) so that module
can import this one without a cycle.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.db import db

logger = logging.getLogger(__name__)

TRIGGERS = ("cli", "refresh_route", "cold_start")
STATUS_OK = "ok"
STATUS_UNHEALTHY = "unhealthy"
STATUS_FAILED = "failed"


# ---------------------------------------------------------------------------
# Health (pure)
# ---------------------------------------------------------------------------

def assess_health(
    report,
    *,
    max_failed_terms: int | None = None,
    max_skip_rate: float | None = None,
) -> tuple[str, list[str]]:
    """``(status, reasons)`` for one finished run. ``status`` is ok | unhealthy.

    Unhealthy when ANY of:

    * zero deals were normalised (the run produced nothing usable);
    * ``terms_failed`` is at or above ``max_failed_terms`` (default
      ``settings.ingestion_max_failed_terms`` = 5);
    * the skip rate (``skipped_total / raw_items``) is above ``max_skip_rate``
      (default ``settings.ingestion_max_skip_rate`` = 0.5);
    * a provider response had no ``items`` key (the client used to swallow that
      as an empty result, indistinguishable from "no deals for this term").

    ``failed`` is not decided here: that is an exception, known only to the
    caller.
    """
    if max_failed_terms is None:
        max_failed_terms = settings.ingestion_max_failed_terms
    if max_skip_rate is None:
        max_skip_rate = settings.ingestion_max_skip_rate

    reasons: list[str] = []

    if report.normalized == 0:
        reasons.append("zero deals normalised")

    failed = len(report.terms_failed)
    if failed >= max_failed_terms:
        reasons.append(f"{failed} search term(s) failed (threshold {max_failed_terms})")

    if report.raw_items > 0:
        rate = report.skipped_total / report.raw_items
        if rate > max_skip_rate:
            reasons.append(
                f"skip rate {rate:.0%} of raw items exceeds {max_skip_rate:.0%}"
            )

    missing = getattr(report, "missing_items_responses", 0)
    if missing:
        reasons.append(f"{missing} provider response(s) had no 'items' key")

    return (STATUS_UNHEALTHY if reasons else STATUS_OK), reasons


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

_RUN_INSERT = """
INSERT INTO ingestion_runs (
    started_at, finished_at, trigger, postal_code, source, dry_run,
    status, health_reasons, error,
    terms_searched, terms_failed, raw_items, normalized, skipped_total,
    deals_inserted, deals_updated, snapshots_inserted,
    regular_price_coverage, unit_null_rate, counters
) VALUES (
    %(started_at)s, %(finished_at)s, %(trigger)s, %(postal_code)s, %(source)s, %(dry_run)s,
    %(status)s, %(health_reasons)s, %(error)s,
    %(terms_searched)s, %(terms_failed)s, %(raw_items)s, %(normalized)s, %(skipped_total)s,
    %(deals_inserted)s, %(deals_updated)s, %(snapshots_inserted)s,
    %(regular_price_coverage)s, %(unit_null_rate)s, %(counters)s::jsonb
)
RETURNING run_id;
"""


def run_row(
    report,
    *,
    trigger: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    health_reasons: list[str],
    error: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """The ``_RUN_INSERT`` parameters for one run. Pure, so it is testable."""
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "trigger": trigger,
        "postal_code": report.postal_code,
        "source": report.source,
        "dry_run": dry_run,
        "status": status,
        "health_reasons": list(health_reasons),
        "error": error,
        "terms_searched": report.terms_searched,
        "terms_failed": len(report.terms_failed),
        "raw_items": report.raw_items,
        "normalized": report.normalized,
        "skipped_total": report.skipped_total,
        "deals_inserted": report.deals_inserted,
        "deals_updated": report.deals_updated,
        "snapshots_inserted": report.snapshots_inserted,
        "regular_price_coverage": report.regular_price_coverage,
        "unit_null_rate": report.unit_null_rate,
        "counters": json.dumps(report.as_dict(), default=str),
    }


def record_run(
    report,
    *,
    trigger: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    status: str,
    health_reasons: list[str] | None = None,
    error: str | None = None,
    dry_run: bool = False,
    database=db,
) -> int | None:
    """Write one ``ingestion_runs`` row. Returns the run id, or None on failure.

    Never raises. This runs after the ingestion's own transaction has ended and
    uses a fresh connection, so nothing here can roll the ingestion back; and a
    missing table (020 not applied yet), a dead Neon connection or a bad value
    is logged and swallowed rather than turning a successful ingestion into a
    500.
    """
    try:
        params = run_row(
            report,
            trigger=trigger,
            started_at=started_at,
            finished_at=finished_at or datetime.now(timezone.utc),
            status=status,
            health_reasons=health_reasons or [],
            error=error,
            dry_run=dry_run,
        )
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(_RUN_INSERT, params)
                row = cursor.fetchone()
        return row["run_id"] if row else None
    except Exception:  # noqa: BLE001 - recording must never fail the run
        logger.exception(
            "Could not record ingestion run for %s (status=%s); the ingestion "
            "itself is unaffected",
            getattr(report, "postal_code", "?"), status,
        )
        return None


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

_RUN_COLUMNS = """
run_id, started_at, finished_at, trigger, postal_code, source, status,
health_reasons, error, terms_searched, terms_failed, raw_items, normalized,
skipped_total, deals_inserted, deals_updated, snapshots_inserted,
regular_price_coverage, unit_null_rate
"""

_LATEST_RUN = f"""
SELECT {_RUN_COLUMNS}
FROM ingestion_runs
WHERE postal_code = %(postal_code)s AND NOT dry_run
ORDER BY finished_at DESC
LIMIT 1;
"""

_LATEST_OK_RUN = f"""
SELECT {_RUN_COLUMNS}
FROM ingestion_runs
WHERE postal_code = %(postal_code)s AND status = 'ok' AND NOT dry_run
ORDER BY finished_at DESC
LIMIT 1;
"""

# "Live" matches what serving reads: inside its validity window, at an active
# store. Seeded and ingested deals both count.
_LIVE_DEAL_COUNT = """
SELECT count(*) AS n
FROM deals d
JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = %(postal_code)s
  AND s.is_active = true
  AND d.valid_from <= CURRENT_DATE
  AND d.valid_until >= CURRENT_DATE;
"""


def _fetch_one(sql: str, postal_code: str, database) -> dict[str, Any] | None:
    with database.get_cursor() as cursor:
        cursor.execute(sql, {"postal_code": postal_code})
        row = cursor.fetchone()
    return dict(row) if row else None


def latest_run(postal_code: str, *, database=db) -> dict[str, Any] | None:
    """Most recent recorded run of any status, or None."""
    return _fetch_one(_LATEST_RUN, postal_code, database)


def latest_successful_run(postal_code: str, *, database=db) -> dict[str, Any] | None:
    """Most recent run with status ``ok``, or None."""
    return _fetch_one(_LATEST_OK_RUN, postal_code, database)


def live_deal_count(postal_code: str, *, database=db) -> int:
    """Deals currently valid at an active store in this postal code."""
    row = _fetch_one(_LIVE_DEAL_COUNT, postal_code, database)
    return int(row["n"]) if row else 0


def evaluate_freshness(
    last_ok_run: dict[str, Any] | None,
    live_deals: int,
    *,
    now: datetime | None = None,
    max_age_days: int | None = None,
) -> tuple[bool, list[str], timedelta | None]:
    """``(healthy, problems, age_of_last_ok_run)``. Pure.

    Unhealthy when there has never been an ok run, the last ok run is older
    than ``max_age_days`` (default ``settings.ingestion_stale_days`` = 8), or
    there are zero live deals.
    """
    now = now or datetime.now(timezone.utc)
    if max_age_days is None:
        max_age_days = settings.ingestion_stale_days

    problems: list[str] = []
    age: timedelta | None = None

    if last_ok_run is None:
        problems.append("no successful ingestion run recorded")
    else:
        age = now - last_ok_run["finished_at"]
        if age > timedelta(days=max_age_days):
            problems.append(
                f"last successful run is {age.days} day(s) old (limit {max_age_days})"
            )

    if live_deals == 0:
        problems.append("0 live deals")

    return (not problems), problems, age
