#!/usr/bin/env python
"""Capture a frozen evaluation snapshot from the database. Read-only.

Writes everything a recipe-generation run reads from Postgres -- the deals
live at ``--as-of`` for ``--postal-code``, their stores, their
``deal_products`` links, the linked products and every pantry staple, with
the curated ``contains_*`` attributes -- to a canonical, versioned JSON file
that ``app.eval.snapshots`` loads into offline ``GraphDeps`` fakes.

Read-only, three ways over:

1. the session is opened with ``default_transaction_read_only=on``;
2. the one transaction is ``REPEATABLE READ, READ ONLY`` (one consistent
   view of the data) and the script checks ``transaction_read_only`` is
   ``on`` before reading anything;
3. every statement goes through :func:`_read`, which refuses anything that
   is not a ``SELECT`` or ``SHOW``.

The transaction is rolled back at the end. Postgres itself rejects any write
in a read-only transaction, so even a bug here cannot change the database.

What "as of" means: deals are selected by their validity window
(``valid_from <= as_of <= valid_until``), exactly as the app's
``CURRENT_DATE`` filter would have selected them that day -- provided the
rows still exist. Products, attributes and links are the database's state
*now*: attributes have no history table to rewind, so a past-week capture
replays that week's deals under today's curated attributes.

Usage::

    .venv/bin/python scripts/capture_snapshot.py \\
        --name week-2026-10-16 --as-of 2026-10-16 \\
        --out app/tests/fixtures/snapshots/week-2026-10-16.json

No user rows are read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from decimal import Decimal
from typing import Any, Iterable

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from app.eval.snapshots import (  # noqa: E402
    DEAL_FIELDS,
    DEAL_PRODUCT_FIELDS,
    FORMAT_VERSION,
    PRODUCT_FIELDS,
    STORE_FIELDS,
    write_snapshot,
)

_ALLOWED = ("SELECT", "SHOW")


class NotReadOnly(RuntimeError):
    """The capture was about to run something that is not a read."""


def _read(cur, sql: str, params: dict | None = None) -> list[dict]:
    """Execute one read and return its rows as dicts. Refuses non-reads."""
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in _ALLOWED or ";" in sql.strip().rstrip(";"):
        raise NotReadOnly(f"refusing to execute a non-read statement: {sql.strip()[:60]!r}")
    cur.execute(sql, params or {})
    cols = [c.name if hasattr(c, "name") else c[0] for c in cur.description]
    return [dict(zip(cols, row)) if not isinstance(row, dict) else dict(row)
            for row in cur.fetchall()]


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def _rows(rows: Iterable[dict], fields: Iterable[str]) -> list[dict]:
    return [{f: _plain(r[f]) for f in fields} for r in rows]


def _columns(alias: str, fields: Iterable[str]) -> str:
    # Column names come from the loader's own field specs, never from input.
    return ", ".join(f"{alias}.{f}" for f in fields)


DEALS_SQL = f"""
SELECT {_columns('d', DEAL_FIELDS)}
FROM deals d
JOIN stores s ON s.store_id = d.store_id
WHERE s.postal_code = ANY(%(postal_codes)s)
  AND d.valid_from <= %(as_of)s
  AND d.valid_until >= %(as_of)s
ORDER BY d.deal_id
"""

STORES_SQL = f"""
SELECT {_columns('s', STORE_FIELDS)}
FROM stores s
WHERE s.store_id = ANY(%(store_ids)s)
ORDER BY s.store_id
"""

LINKS_SQL = f"""
SELECT {_columns('dp', DEAL_PRODUCT_FIELDS)}
FROM deal_products dp
WHERE dp.deal_id = ANY(%(deal_ids)s)
ORDER BY dp.deal_id, dp.product_id
"""

PRODUCTS_SQL = f"""
SELECT {_columns('p', PRODUCT_FIELDS)}
FROM products p
WHERE p.product_id = ANY(%(product_ids)s) OR p.assumed_on_hand
ORDER BY p.product_id
"""

CLOCK_SQL = "SELECT transaction_timestamp() AT TIME ZONE 'UTC' AS captured_at"
MIGRATION_SQL = "SELECT max(filename) AS latest FROM schema_migrations"


def capture(cur, *, name: str, as_of: dt.date, postal_codes: list[str],
            source: str, notes: str | None = None) -> dict:
    """Read one snapshot through ``cur`` (inside a read-only transaction)."""
    flag = _read(cur, "SHOW transaction_read_only")
    if not flag or str(next(iter(flag[0].values()))).lower() != "on":
        raise NotReadOnly("transaction is not read-only; refusing to capture")

    captured_at = _read(cur, CLOCK_SQL)[0]["captured_at"]
    migration = _read(cur, MIGRATION_SQL)[0]["latest"]

    deals = _read(cur, DEALS_SQL, {"postal_codes": postal_codes, "as_of": as_of})
    deal_ids = [d["deal_id"] for d in deals]
    stores = _read(cur, STORES_SQL, {"store_ids": sorted({d["store_id"] for d in deals})})
    links = _read(cur, LINKS_SQL, {"deal_ids": deal_ids})
    products = _read(cur, PRODUCTS_SQL,
                     {"product_ids": sorted({l["product_id"] for l in links})})

    if isinstance(captured_at, dt.datetime) and captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=dt.timezone.utc)
    return {
        "meta": {
            "name": name,
            "format_version": FORMAT_VERSION,
            "captured_at": _plain(captured_at),
            "source": source,
            "as_of": as_of.isoformat(),
            "postal_codes": sorted(postal_codes),
            "schema_migration": migration,
            "notes": notes,
        },
        "stores": _rows(stores, STORE_FIELDS),
        "deals": _rows(deals, DEAL_FIELDS),
        "deal_products": _rows(links, DEAL_PRODUCT_FIELDS),
        "products": _rows(products, PRODUCT_FIELDS),
    }


def open_read_only(url: str):
    """A connection whose every transaction is read-only."""
    import psycopg

    conn = psycopg.connect(url, autocommit=False,
                           options="-c default_transaction_read_only=on")
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True  # BEGIN ... READ ONLY
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--as-of", required=True, type=dt.date.fromisoformat,
                        help="date whose live deals are captured (YYYY-MM-DD)")
    parser.add_argument("--postal-code", action="append", dest="postal_codes",
                        help="repeatable; default M5V3A8")
    parser.add_argument("--source", default="neon",
                        help="label recorded in meta.source (never the URL)")
    parser.add_argument("--notes", default=None)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv(os.path.join(REPO, ".env"))
    url = os.environ["DATABASE_URL"]
    conn = open_read_only(url)
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            snap = capture(cur, name=args.name, as_of=args.as_of,
                           postal_codes=args.postal_codes or ["M5V3A8"],
                           source=args.source, notes=args.notes)
    finally:
        conn.rollback()
        conn.close()

    path = write_snapshot(snap, args.out)
    print(f"{path}: {len(snap['deals'])} deals, {len(snap['stores'])} stores, "
          f"{len(snap['deal_products'])} links, {len(snap['products'])} products")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
