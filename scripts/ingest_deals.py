#!/usr/bin/env python
"""Manually trigger deal ingestion for one or more postal codes.

The API equivalent is ``POST /postal-code/refresh``; this is the path that
does not need the server running. There is deliberately **no scheduler** in
this project — see the note in ``app/routes/stores.py::refresh_deals``. Run
this weekly, when flyers turn over.

    # what would be fetched, no network, no writes
    .venv/bin/python scripts/ingest_deals.py --list-staples

    # one term, hit the provider, print the normalised rows, write nothing
    .venv/bin/python scripts/ingest_deals.py M5V3A8 --terms "chicken breast" --dry-run

    # the real thing
    .venv/bin/python scripts/ingest_deals.py M5V3A8

Prerequisites:
  * ``DEAL_INGESTION_ENABLED=true`` in ``.env`` (off by default — the provider
    is an unsanctioned, keyless endpoint; read ``app/services/flipp_client.py``)
  * ``migrations/001``-``003`` applied to whatever ``DATABASE_URL`` points at
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.services.flipp_ingestion import collect_deals, run_ingestion  # noqa: E402
from app.services.flipp_staples import STAPLES, staple_terms  # noqa: E402


def _list_staples() -> int:
    by_category: dict[str, list[str]] = {}
    for staple in STAPLES:
        by_category.setdefault(staple.category, []).append(staple.term)

    print(f"{len(STAPLES)} staple term(s) — edit app/services/flipp_staples.py\n")
    for category in sorted(by_category):
        print(f"  {category}")
        for term in by_category[category]:
            print(f"      {term}")
    print(
        "\nIngestion coverage is exactly these terms. The provider is "
        "search-by-term only;\na flyer cannot be enumerated."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("postal_codes", nargs="*", metavar="POSTAL_CODE",
                        help="one or more postal codes, e.g. M5V3A8")
    parser.add_argument("--terms", nargs="+", default=None,
                        help="override the staple list for this run")
    parser.add_argument("--limit-terms", type=int, default=None, metavar="N",
                        help="use only the first N terms (cheap smoke test)")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and normalise, print the result, write nothing")
    parser.add_argument("--list-staples", action="store_true",
                        help="print the staple list and exit; no network, no DB")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if args.list_staples:
        return _list_staples()

    if not args.postal_codes:
        parser.error("at least one POSTAL_CODE is required (or use --list-staples)")

    if not settings.deal_ingestion_enabled:
        print(
            "Refusing to run: DEAL_INGESTION_ENABLED is not set.\n\n"
            "Ingestion talks to https://backflipp.wishabi.com — the keyless "
            "backend behind\nflipp.com's own search box. It is undocumented, "
            "unlicensed and can be blocked at\nany time, so it is opt-in. Read "
            "app/services/flipp_client.py, then set\nDEAL_INGESTION_ENABLED=true "
            "in .env.\n\n"
            "Also apply migrations/001-003 first, or every INSERT will fail on "
            "an unknown\ncolumn.",
            file=sys.stderr,
        )
        return 2

    terms = args.terms if args.terms is not None else staple_terms()
    if args.limit_terms is not None:
        terms = terms[: args.limit_terms]

    exit_code = 0
    for postal_code in args.postal_codes:
        postal_code = postal_code.replace(" ", "").upper()
        print(f"\n=== {postal_code} — {len(terms)} term(s) ===")

        if args.dry_run:
            from app.services.flipp_client import FlippSearchClient

            deals, report = collect_deals(FlippSearchClient(), postal_code, terms=terms)
            for deal in deals:
                print(
                    f"  {deal.merchant_name:<30} {deal.product_name[:48]:<50} "
                    f"${deal.sale_price:>7.2f}"
                    f"{('  (reg $%.2f)' % deal.regular_price) if deal.regular_price else ''}"
                    f"  [{deal.price_basis}]"
                )
            print("\n  DRY RUN — nothing written.")
            print(json.dumps(report.as_dict(), indent=2, default=str))
            continue

        report = run_ingestion(postal_code, terms=terms)
        print(json.dumps(report.as_dict(), indent=2, default=str))
        if report.terms_failed:
            print(f"  WARNING: {len(report.terms_failed)} term(s) failed: "
                  f"{', '.join(report.terms_failed)}")
            exit_code = 1
        if report.normalized and report.regular_price_coverage < 0.5:
            print(
                f"  NOTE: only {report.regular_price_coverage:.0%} of these deals "
                "carry a regular price,\n        so estimated_savings is unreliable "
                "for this batch. The self-derived\n        baseline in "
                "app/services/price_baseline.py needs 4-8 weeks of runs\n        "
                "before it can replace it."
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
