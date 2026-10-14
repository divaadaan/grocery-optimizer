#!/usr/bin/env python
"""Review product proposals, and run the deterministic link step.

The pipeline, and where this sits in it::

    ingest  ->  link  ->  propose  ->  REVIEW  ->  link
    (flipp)     (det.)    (claude)    (you)       (det.)

Nothing an LLM proposed affects a single user until it passes through here.
``link`` only ever follows ``status='confirmed'`` rows, so the review step is
the gate, not a formality.

Review is grouped by product, not by alias
------------------------------------------
One product arrives under many flyer strings: 91 ingested names resolved to 21
products on the first pass, so reviewing alias-by-alias meant 88 decisions to
establish 21 facts. Worse, it scaled the wrong way -- the full staples list
projects to ~2,000 names, which alias-by-alias is a data-entry job nobody
finishes, and an abandoned review means deals stay withheld.

So a decision is made once per product and applies to every string that maps
to it. The dietary judgement is a property of the product, which is exactly
what makes this safe to batch: confirming "milk contains dairy" once is not a
weaker statement than confirming it fifteen times. Alias-level override is
still there (``p``) for a group with one string that does not belong.

Why there is no ``--auto-confirm``
----------------------------------
It was considered and deliberately left out. Auto-confirming above a
confidence threshold would hand the allergen decision back to the model, and
model confidence is not calibrated to allergen risk. Grouping is the right way
to make review cheap; skipping review is not.

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

#: How many of a group's aliases to print before truncating.
ALIAS_PREVIEW = 6


def _reviewer() -> str:
    return os.environ.get("REVIEWER") or os.environ.get("USER") or "unknown"


def _pending(limit: int = 5000) -> list[dict]:
    with db.get_cursor() as cursor:
        cursor.execute(
            """
            SELECT a.alias_id, a.normalized_name, a.raw_example, a.confidence,
                   a.proposal_reason, a.times_seen, a.source,
                   p.product_id, p.canonical_name, p.category,
                   p.status AS product_status, p.is_pantry_staple,
                   p.contains_meat, p.contains_fish, p.contains_dairy,
                   p.contains_egg, p.contains_nuts
            FROM product_aliases a
            LEFT JOIN products p ON p.product_id = a.product_id
            WHERE a.status = 'proposed'
            ORDER BY a.times_seen DESC, a.normalized_name
            LIMIT %(limit)s
            """,
            {"limit": limit},
        )
        return [dict(r) for r in cursor.fetchall()]


def _grouped(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split pending aliases into product groups and the unplaceable ones.

    Groups are ordered by total ``times_seen``: a group seen often is being
    withheld from users week after week, so it is worth deciding first.
    """
    groups: dict[int, dict] = {}
    orphans: list[dict] = []

    for row in rows:
        if row["product_id"] is None:
            orphans.append(row)
            continue
        group = groups.setdefault(
            row["product_id"],
            {
                "product_id": row["product_id"],
                "canonical_name": row["canonical_name"],
                "category": row["category"],
                "product_status": row["product_status"],
                "is_pantry_staple": row["is_pantry_staple"],
                **{attr: row[attr] for attr in products.ATTRIBUTES},
                "aliases": [],
            },
        )
        group["aliases"].append(row)

    ordered = sorted(
        groups.values(),
        key=lambda g: (-sum(a["times_seen"] for a in g["aliases"]),
                       g["canonical_name"] or ""),
    )
    orphans.sort(key=lambda r: (-r["times_seen"], r["raw_example"]))
    return ordered, orphans


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


def _clears(row: dict) -> str:
    """Which restriction labels this product's attributes clear."""
    if row.get("product_id") is None:
        return "nothing - withheld"
    ok = [
        label
        for label in ("vegan", "vegetarian", "pescatarian", "no_nuts")
        if products.product_allows(row, [label])
    ]
    return ", ".join(ok) if ok else "none of them"


def cmd_list() -> int:
    groups, orphans = _grouped(_pending())
    if not groups and not orphans:
        print("No pending proposals.")
        return 0

    in_groups = sum(len(g["aliases"]) for g in groups)
    total = in_groups + len(orphans)
    # Spell the arithmetic out. "N pending in M groups plus K unplaceable"
    # reads as N+K, which is wrong and looks like missing rows.
    parts = [f"{in_groups} across {len(groups)} product group(s)"]
    if orphans:
        parts.append(f"{len(orphans)} unplaceable")
    print(f"{total} pending string(s): {', '.join(parts)}.\n")

    for group in groups:
        seen = sum(a["times_seen"] for a in group["aliases"])
        print(f"  {group['canonical_name']}  [{group['category']}]"
              f"  -- {len(group['aliases'])} alias(es), seen {seen}x")
        print(f"      contains: {_attr_summary(group)}")
        print(f"      clears:   {_clears(group)}")

    if orphans:
        print(f"\n  (unplaceable: {len(orphans)} string(s) with no product -- "
              f"multi-product flyer copy and garbled text)")
    print("\nRun with --review to decide on them.")
    return 0


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------

def _confirm_group(product_id: int, alias_ids: list[int], reviewer: str) -> None:
    """Confirm the product and the given aliases in one transaction.

    The product is promoted alongside its aliases because the reviewer just
    verified these attributes -- requiring a separate product confirmation
    would be busywork that invites rubber-stamping.
    """
    now = datetime.now(timezone.utc)
    with db.get_cursor() as cursor:
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
            WHERE alias_id = ANY(%(ids)s)
            """,
            {"ids": alias_ids, "who": reviewer, "now": now},
        )


def _reject_aliases(alias_ids: list[int], reviewer: str) -> None:
    now = datetime.now(timezone.utc)
    with db.get_cursor() as cursor:
        cursor.execute(
            """
            UPDATE product_aliases
            SET status = 'rejected', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE alias_id = ANY(%(ids)s)
            """,
            {"ids": alias_ids, "who": reviewer, "now": now},
        )


def _reject_product(product_id: int, reviewer: str) -> None:
    """Reject a product that has no remaining proposed or confirmed aliases.

    Keeps the live-name unique index free, so a better proposal for the same
    canonical name can be created later.
    """
    now = datetime.now(timezone.utc)
    with db.get_cursor() as cursor:
        cursor.execute(
            """
            UPDATE products
            SET status = 'rejected', reviewed_by = %(who)s, reviewed_at = %(now)s
            WHERE product_id = %(pid)s
              AND status = 'proposed'
              AND NOT EXISTS (
                  SELECT 1 FROM product_aliases
                  WHERE product_id = %(pid)s AND status <> 'rejected'
              )
            """,
            {"pid": product_id, "who": reviewer, "now": now},
        )


def _toggle_attributes(product_id: int, row: dict) -> bool:
    """Flip attributes on a proposed product. Returns True if anything changed."""
    # Explicit mapping rather than zipping letters against the attribute
    # tuple: the pairing has to stay obvious to whoever reads this next.
    keys = {
        "m": "contains_meat",
        "f": "contains_fish",
        "d": "contains_dairy",
        "e": "contains_egg",
        "n": "contains_nuts",
    }
    print("  Current labels:")
    for letter, attr in keys.items():
        state = "YES" if row[attr] else "no "
        print(f"    {letter} = contains {ATTR_LABEL[attr]:<16} {state}")
    print("  Type the letters you want to FLIP, e.g. 'dn' flips dairy and nuts.")
    print("  Press Enter alone to leave them as they are.")
    raw = input("  flip> ").strip().lower()

    flips = {keys[c] for c in raw if c in keys}
    unknown = {c for c in raw if c not in keys and not c.isspace()}
    if unknown:
        print(f"  ignored unrecognised letter(s): {''.join(sorted(unknown))}")
    if not flips:
        print("  unchanged")
        return False

    updates = {attr: not bool(row[attr]) for attr in flips}
    sets = ", ".join(f"{attr} = %({attr})s" for attr in updates)
    with db.get_cursor() as cursor:
        cursor.execute(
            f"UPDATE products SET {sets} WHERE product_id = %(pid)s",
            {**updates, "pid": product_id},
        )
    for attr, value in updates.items():
        row[attr] = value
        print(f"  {ATTR_LABEL[attr]} -> {value}")
    return True


# --------------------------------------------------------------------------
# The review pass
# --------------------------------------------------------------------------

LEGEND = """\
For each product, say whether the dietary labels shown are correct. One
decision covers every flyer string that maps to that product.

  y = YES, labels are right  -> confirms the product and ALL its strings;
                                --link then serves them to users whose
                                restrictions those labels clear
  n = NO, wrong or unusable  -> rejects all its strings; they stay withheld
                                and will not come back in next week's queue
  e = EDIT the attributes    -> flip meat/fish/dairy/egg/nuts, then decide
  p = PICK strings one by one-> for a group where one string does not belong
  s = SKIP, decide later     -> no decision; stays in the queue
  q = QUIT                   -> stops here, keeps what you decided

Nothing reaches a user until you confirm it. When unsure, skip or reject --
a withheld deal costs one offer, a wrong label costs more."""

_WORDS = {"yes": "y", "no": "n", "edit": "e", "pick": "p",
          "skip": "s", "quit": "q", "exit": "q"}


def _ask(prompt: str, allowed: str) -> str:
    while True:
        answer = input(prompt).strip().lower()
        answer = _WORDS.get(answer, answer)
        if answer in set(allowed):
            return answer
        print(f"  please answer one of {'/'.join(allowed)} (or type the word)")


def _show_group(group: dict) -> None:
    aliases = group["aliases"]
    seen = sum(a["times_seen"] for a in aliases)
    print("-" * 72)
    print(f"  product    {group['canonical_name']}  [{group['category']}]"
          f"  ({group['product_status']})")
    print(f"  contains   {_attr_summary(group)}")
    print(f"  clears     {_clears(group)}")
    if group["is_pantry_staple"]:
        print("  pantry     yes")
    print(f"  strings    {len(aliases)} mapping to it, seen {seen}x total")
    for alias in aliases[:ALIAS_PREVIEW]:
        conf = alias["confidence"]
        conf_s = f"{float(conf):.2f}" if conf is not None else " n/a"
        print(f"      x{alias['times_seen']:<2} conf {conf_s}  "
              f"{alias['raw_example'][:56]}")
    if len(aliases) > ALIAS_PREVIEW:
        print(f"      ... and {len(aliases) - ALIAS_PREVIEW} more")

    reasons = {a["proposal_reason"] for a in aliases if a["proposal_reason"]}
    if reasons:
        print(f"  reason     {sorted(reasons)[0]}")

    # The oracle alarm, surfaced where a human is already looking at the label
    # rather than used as a veto at request time.
    disagreements = products.oracle_disagreement(group)
    if disagreements:
        print(f"  ** the substring oracle disagrees for {disagreements} -- check "
              f"the labels against the name before confirming")


def _pick_individually(group: dict, reviewer: str) -> int:
    """Per-alias decisions within one product group."""
    decided = 0
    confirm_ids: list[int] = []
    reject_ids: list[int] = []
    print(f"  Deciding {len(group['aliases'])} string(s) one at a time.")
    for alias in group["aliases"]:
        print(f"    {alias['raw_example'][:64]}")
        answer = _ask(f"      is this {group['canonical_name']}? "
                      f"[y/n/s]> ", "yns")
        if answer == "y":
            confirm_ids.append(alias["alias_id"])
        elif answer == "n":
            reject_ids.append(alias["alias_id"])
    if confirm_ids:
        _confirm_group(group["product_id"], confirm_ids, reviewer)
        decided += len(confirm_ids)
        print(f"    confirmed {len(confirm_ids)}")
    if reject_ids:
        _reject_aliases(reject_ids, reviewer)
        decided += len(reject_ids)
        print(f"    rejected {len(reject_ids)}")
    if not confirm_ids:
        _reject_product(group["product_id"], reviewer)
    return decided


def cmd_review() -> int:
    groups, orphans = _grouped(_pending())
    if not groups and not orphans:
        print("No pending proposals.")
        return 0

    reviewer = _reviewer()
    total_aliases = sum(len(g["aliases"]) for g in groups) + len(orphans)
    print(f"Reviewing {len(groups)} product group(s) covering {total_aliases} "
          f"flyer string(s), as {reviewer!r}.\n")
    print(LEGEND + "\n")

    decided = 0
    quit_early = False

    for group in groups:
        _show_group(group)
        answer = _ask("  labels correct? yes / no / edit / pick / skip / quit "
                      "[y/n/e/p/s/q]> ", "ynepsq")

        if answer == "q":
            quit_early = True
            break
        if answer == "s":
            continue
        if answer == "p":
            decided += _pick_individually(group, reviewer)
            continue
        if answer == "e":
            _toggle_attributes(group["product_id"], group)
            print(f"  now clears: {_clears(group)}")
            if _ask("  confirm this product and all its strings? [y/n]> ",
                    "yn") != "y":
                continue
            answer = "y"
        if answer == "y":
            ids = [a["alias_id"] for a in group["aliases"]]
            _confirm_group(group["product_id"], ids, reviewer)
            decided += len(ids)
            print(f"  confirmed {group['canonical_name']} and "
                  f"{len(ids)} string(s)")
            continue
        # answer == "n"
        ids = [a["alias_id"] for a in group["aliases"]]
        _reject_aliases(ids, reviewer)
        _reject_product(group["product_id"], reviewer)
        decided += len(ids)
        print(f"  rejected {group['canonical_name']} and {len(ids)} string(s)")

    if orphans and not quit_early:
        print("-" * 72)
        print(f"  {len(orphans)} string(s) could not be placed on any product.")
        print("  These are multi-product flyer tiles and garbled copy -- one tile")
        print("  advertising several products at one price. See the debt entry in")
        print("  ROADMAP.md; rejecting them withholds them permanently and stops")
        print("  them re-queueing every week.")
        for alias in orphans[:ALIAS_PREVIEW]:
            print(f"      x{alias['times_seen']:<2} {alias['raw_example'][:60]}")
        if len(orphans) > ALIAS_PREVIEW:
            print(f"      ... and {len(orphans) - ALIAS_PREVIEW} more")
        answer = _ask("  reject all of them? yes / skip [y/s]> ", "ys")
        if answer == "y":
            _reject_aliases([a["alias_id"] for a in orphans], reviewer)
            decided += len(orphans)
            print(f"  rejected {len(orphans)}")

    print("-" * 72)
    print(f"Decided {decided} string(s).")
    if decided:
        print("Run --link to apply the new mappings to existing deals.")
    return 0


# --------------------------------------------------------------------------
# Non-interactive commands
# --------------------------------------------------------------------------

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
              f"${row['min_sale_price']}-{row['max_sale_price']}  "
              f"{row['product_name'][:50]}")
    return 0


def cmd_audit() -> int:
    flagged = products.log_oracle_disagreements()
    if not flagged:
        print("No confirmed product contradicts the substring oracle.")
        return 0
    print(f"{len(flagged)} confirmed product(s) disagree with the substring oracle.")
    print("Usually the oracle is wrong -- that is why this table exists -- but")
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
    group.add_argument("--review", action="store_true",
                       help="decide on them interactively, grouped by product")
    group.add_argument("--link", action="store_true",
                       help="apply confirmed mappings to deals")
    group.add_argument("--unmapped", action="store_true",
                       help="deal names with no product")
    group.add_argument("--audit", action="store_true",
                       help="attributes vs the substring oracle")
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
