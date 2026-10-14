#!/usr/bin/env python
"""Review product proposals, and run the deterministic link step.

The pipeline, and where this sits in it::

    ingest  ->  link  ->  propose  ->  REVIEW  ->  link
    (flipp)     (det.)    (claude)    (you)       (det.)

Nothing an LLM proposed affects a single user until it passes through here.
``link`` only ever follows ``status='confirmed'`` rows, so the review step is
the gate, not a formality.

Why there is no ``--auto-confirm`` flag
---------------------------------------
It was considered and deliberately left out. Auto-confirming above a
confidence threshold would hand the allergen decision back to the model, which
is the exact thing this architecture exists to prevent — and model confidence
is not calibrated to allergen risk. If the queue is long, review the
most-seen items first (the default order) and leave the tail withheld; a
withheld deal costs a user one offer, a wrong label can cost them a hospital
visit.

Usage::

    python scripts/review_products.py --list
    python scripts/review_products.py --review
    python scripts/review_products.py --link
    python scripts/review_products.py --audit
    python scripts/review_products.py --unmapped
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import db  # noqa: E402
from app.services import products  # noqa: E402

ATTR_LABEL = {
    "contains_meat": "meat",
    "contains_fish": "fish/shellfish",
    "contains_dairy": "dairy",
    "contains_egg": "egg",
    "contains_nuts": "nuts",
}


def _reviewer() -> str:
    return os.environ.get("REVIEWER") or os.environ.get("USER") or "unknown"


def _pending(limit: int = 500) -> list[dict]:
    """Proposed aliases, most-frequently-seen first.

    Ordering by ``times_seen`` is not cosmetic: a still-proposed alias with a
    high count is an item being withheld from restricted users week after week,
    so it is the one worth reviewing first.
    """
    with db.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT a.alias_id, a.normalized_name, a.raw_example, a.confidence,
                   a.proposal_reason, a.times_seen, a.source,
                   p.product_id, p.canonical_name, p.category, p.status AS product_status,
                   p.contains_meat, p.contains_fish, p.contains_dairy,
                   p.contains_egg, p.contains_nuts, p.is_pantry_staple
            FROM product_aliases a
            LEFT JOIN products p ON p.product_id = a.product_id
            WHERE a.status = 'proposed'
            ORDER BY a.times_seen DESC, a.normalized_name
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )
        return [dict(r) for r in cursor.fetchall()]


def _attr_summary(row: dict) -> str:
    """Human-readable attribute summary.

    A row with no linked product has NULL attributes from the LEFT JOIN, and
    rendering that as "no animal products or nuts" would display an
    unplaceable item as if it were safe. It is the opposite: nothing unlinked
    is served to a restricted user at all.
    """
    if row.get("product_id") is None:
        return "UNKNOWN - no product linked, withheld from all restricted users"
    present = [ATTR_LABEL[a] for a in products.ATTRIBUTES if row.get(a)]
    return ", ".join(present) if present else "no animal products or nuts"


def cmd_list() -> int:
    rows = _pending()
    if not rows:
        print("No pending proposals.")
        return 0
    print(f"{len(rows)} pending proposal(s), most-seen first:\n")
    for row in rows:
        target = row["canonical_name"] or "(unplaceable)"
        conf = row["confidence"]
        conf_s = f"{float(conf):.2f}" if conf is not None else " n/a"
        print(f"  [{row['alias_id']:>5}] x{row['times_seen']:<3} conf {conf_s}  "
              f"{row['raw_example'][:44]:<44} -> {target}")
        print(f"          contains: {_attr_summary(row)}")
    print("\nRun with --review to decide on them.")
    return 0


def _confirm(alias_id: int, product_id: int | None, reviewer: str) -> None:
    now = datetime.now(timezone.utc)
    with db.get_cursor() as cursor:
        if product_id is not None:
            # Promote the product too. One decision, not two — the reviewer
            # just verified these attributes, so requiring a separate product
            # confirmation would be busywork that invites rubber-stamping.
            cursor.execute(
                """
                UPDATE products
                SET status = 'confirmed', reviewed_by = %(who)s, reviewed_at = %(now)s
                WHERE product_id = %(pid)s AND status <> 'confirmed'
                """,
                {"pid": product_id, "who": reviewer, "now": now},
            )
        cursor.execute(
            """
            UPDATE product_aliases
            SET status = 'confirmed', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE alias_id = %(aid)s
            """,
            {"aid": alias_id, "who": reviewer, "now": now},
        )


def _reject(alias_id: int, reviewer: str) -> None:
    now = datetime.now(timezone.utc)
    with db.get_cursor() as cursor:
        cursor.execute(
            """
            UPDATE product_aliases
            SET status = 'rejected', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE alias_id = %(aid)s
            """,
            {"aid": alias_id, "who": reviewer, "now": now},
        )


def _toggle_attributes(product_id: int, reviewer: str) -> None:
    """Edit a proposed product's attributes before confirming it."""
    with db.get_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM products WHERE product_id = %(pid)s", {"pid": product_id}
        )
        row = dict(cursor.fetchone())

    print("  Toggle attributes. Enter the letters to flip, blank to keep:")
    keys = {a[0]: attr for a, attr in zip("mfdens", products.ATTRIBUTES)}
    for letter, attr in keys.items():
        print(f"    {letter} = {ATTR_LABEL[attr]:<16} currently {bool(row[attr])}")
    raw = input("  flip> ").strip().lower()
    flips = {keys[c] for c in raw if c in keys}
    if not flips:
        print("  unchanged")
        return

    updates = {attr: not bool(row[attr]) for attr in flips}
    sets = ", ".join(f"{attr} = %({attr})s" for attr in updates)
    with db.get_cursor() as cursor:
        cursor.execute(
            f"UPDATE products SET {sets} WHERE product_id = %(pid)s",
            {**updates, "pid": product_id},
        )
    for attr, value in updates.items():
        print(f"  {ATTR_LABEL[attr]} -> {value}")


def cmd_review() -> int:
    rows = _pending()
    if not rows:
        print("No pending proposals.")
        return 0

    reviewer = _reviewer()
    print(f"Reviewing {len(rows)} proposal(s) as {reviewer!r}.")
    print("  [y] confirm   [n] reject   [e] edit attributes   [s] skip   [q] quit\n")

    decided = 0
    for row in rows:
        print("-" * 72)
        print(f"  raw        {row['raw_example']}")
        print(f"  normalized {row['normalized_name']}")
        print(f"  seen       {row['times_seen']}x   confidence "
              f"{row['confidence'] if row['confidence'] is not None else 'n/a'}"
              f"   source {row['source']}")
        if row["product_id"] is None:
            print("  product    (the model could not place this — reject, or map it by hand)")
        else:
            print(f"  product    {row['canonical_name']}  [{row['category']}]"
                  f"  ({row['product_status']})")
            print(f"  contains   {_attr_summary(row)}")
        if row["proposal_reason"]:
            print(f"  reason     {row['proposal_reason']}")

        # The oracle alarm. Surfaced here, where a human is already looking,
        # rather than used as a veto at request time.
        if row["product_id"] is not None:
            disagreements = products.oracle_disagreement(row)
            if disagreements:
                print(f"  ** the substring oracle disagrees for {disagreements} — "
                      f"check the attributes against the name before confirming")

        while True:
            choice = input("  [y/n/e/s/q]> ").strip().lower()
            if choice in {"y", "n", "e", "s", "q"}:
                break

        if choice == "q":
            break
        if choice == "s":
            continue
        if choice == "e":
            if row["product_id"] is None:
                print("  nothing to edit — no product attached")
                continue
            _toggle_attributes(row["product_id"], reviewer)
            confirm = input("  confirm now? [y/N]> ").strip().lower()
            if confirm != "y":
                continue
            _confirm(row["alias_id"], row["product_id"], reviewer)
            decided += 1
            print("  confirmed")
            continue
        if choice == "y":
            if row["product_id"] is None:
                print("  cannot confirm an alias with no product — rejecting instead")
                _reject(row["alias_id"], reviewer)
            else:
                _confirm(row["alias_id"], row["product_id"], reviewer)
                print("  confirmed")
            decided += 1
            continue
        _reject(row["alias_id"], reviewer)
        decided += 1
        print("  rejected")

    print("-" * 72)
    print(f"Decided {decided} proposal(s).")
    if decided:
        print("Run --link to apply the new mappings to existing deals.")
    return 0


def cmd_link() -> int:
    stats = products.link_deals()
    print(f"  names examined     {stats['examined']}")
    print(f"  deals linked       {stats['linked_deals']}")
    print(f"  snapshots linked   {stats['linked_snapshots']}")
    print(f"  names unmapped     {stats['unmapped']}")
    if stats["unmapped"]:
        print("\nUnmapped names are withheld from users with dietary restrictions.")
        print("Run --unmapped to see them, then propose and review.")
    return 0


def cmd_unmapped() -> int:
    rows = products.unmapped_names()
    if not rows:
        print("Nothing unmapped among currently-valid deals.")
        return 0
    print(f"{len(rows)} unmapped name(s) among currently-valid deals:\n")
    for row in rows:
        print(f"  x{row['deal_count']:<3} {row['store_count']:>2} store(s)  "
              f"${row['min_sale_price']}-{row['max_sale_price']}  {row['product_name'][:50]}")
    return 0


def cmd_audit() -> int:
    flagged = products.log_oracle_disagreements()
    if not flagged:
        print("No confirmed product contradicts the substring oracle.")
        return 0
    print(f"{len(flagged)} confirmed product(s) disagree with the substring oracle.")
    print("Usually the oracle is wrong — that is why this table exists — but")
    print("occasionally it is a mislabelled product, and only looking tells you.\n")
    for item in flagged:
        product = item["product"]
        print(f"  {product['canonical_name']!r} (id={product['product_id']}) "
              f"-> {item['labels']}")
        print(f"      contains: {_attr_summary(product)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="show pending proposals")
    group.add_argument("--review", action="store_true", help="decide on them interactively")
    group.add_argument("--link", action="store_true", help="apply confirmed mappings to deals")
    group.add_argument("--unmapped", action="store_true", help="deal names with no product")
    group.add_argument("--audit", action="store_true", help="attributes vs the substring oracle")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.review:
        return cmd_review()
    if args.link:
        return cmd_link()
    if args.unmapped:
        return cmd_unmapped()
    return cmd_audit()


if __name__ == "__main__":
    raise SystemExit(main())
