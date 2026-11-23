"""Freeze this week's gated deals to JSON so every experiment arm sees identical input.

    .venv/bin/python -m experiments.chef.snapshot --postal L4G3E8 [--restriction kosher ...]

Read-only against DATABASE_URL. The dietary gate is the production one
(``app/services/deal_gate.deal_filter``), applied in SQL -- no LIMIT, no
discount ranking: the experiments decide how deals are surfaced, not the
snapshot.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from app.services.database import DatabaseService
from app.services.deal_gate import deal_filter, gate_scope

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"

QUERY = """
SELECT d.deal_id, d.product_name, d.brand, d.sale_price, d.regular_price,
       d.discount_percentage, d.unit, d.category, d.sale_story, d.pre_price_text,
       d.valid_from, d.valid_until, d.product_id,
       s.store_id, s.name AS store_name, s.chain
FROM deals d
JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = %(postal_code)s
  AND d.valid_until >= CURRENT_DATE
  AND d.valid_from <= CURRENT_DATE
"""


def _plain(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return v


def take_snapshot(postal: str, restrictions: list[str]) -> Path:
    fragment, params = deal_filter(restrictions)
    params = {"postal_code": postal, **params}
    db = DatabaseService()
    with db.db.get_cursor() as cur:
        cur.execute(QUERY + fragment + " ORDER BY d.deal_id", params)
        deals = [{k: _plain(v) for k, v in dict(r).items()} for r in cur.fetchall()]
    scope = gate_scope(restrictions)
    profile = "open" if scope == "open" else "-".join(sorted(restrictions))
    today = dt.date.today().isoformat()
    out = SNAPSHOT_DIR / f"{today}_{postal}_{profile}.json"
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "taken": dt.datetime.now().isoformat(timespec="seconds"),
        "postal_code": postal,
        "dietary_restrictions": restrictions,
        "gate_scope": scope,
        "num_deals": len(deals),
        "deals": deals,
    }, indent=1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--postal", required=True)
    ap.add_argument("--restriction", action="append", default=[])
    a = ap.parse_args()
    path = take_snapshot(a.postal, a.restriction)
    print(path, json.loads(path.read_text())["num_deals"], "deals")


if __name__ == "__main__":
    main()
