#!/usr/bin/env python
"""Apply an owner-approved labelling draft: import, confirm, re-infer, link.

``scripts/review_products.py --review`` is the interactive gate. When the
owner has instead approved a written draft (a labelled file plus its
human-readable table, e.g. ``probe/2026-11-24/``), this applies exactly that
draft and nothing else, in **one transaction**:

1. validate the labelled file against the live unmapped set
   (``product_proposer.load_labels``, strict -- any malformed entry aborts);
2. persist it as proposals (``product_proposer.persist_proposals``);
3. confirm, as ``--reviewer``, only the (name, product) pairs the file
   names (``products.confirm_group``); unmappable entries are rejected;
4. refuse if any product it confirmed has a NULL attribute;
5. fill undecided tile pricing from the deal text
   (``products.reinfer_alias_prices``), then link (``products.link_deals``).

Dry run by default: everything runs and is reported, then rolled back.
``--apply`` commits. The dietary attributes come only from the labelled file
(or, for a match, the existing product's stored attributes); nothing here
reads a deal or product *name* to decide one.

Usage::

    python scripts/apply_product_review.py probe/2026-11-24/labelled.json \\
        --reviewer claude-review-2026-11-24            # dry run
    python scripts/apply_product_review.py ... --apply # commit
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import db  # noqa: E402
from app.services import product_proposer as pp  # noqa: E402
from app.services import products  # noqa: E402


class _Txn:
    """A ``database`` whose cursors all share one connection and one
    transaction, so the whole apply commits or rolls back as a unit."""

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def get_cursor(self, row_factory=None):
        with self.conn.cursor(row_factory=row_factory) as cursor:
            yield cursor


def target_pairs(proposals: Iterable[pp.Proposal]) -> tuple[set, set]:
    """``(normalized_name, lower product name)`` pairs to confirm, and the
    normalized names of unmappable entries to reject."""
    confirm, reject = set(), set()
    for p in proposals:
        if p.unmappable:
            reject.add(p.normalized_name)
            continue
        name = (p.match_existing_product or p.canonical_name or "").strip().lower()
        confirm.add((p.normalized_name, name))
    return confirm, reject


def plan_decisions(alias_rows: Iterable[dict], confirm: set, reject: set
                   ) -> tuple[dict[int, list[int]], list[int], set]:
    """Which proposed aliases to confirm (grouped by product) and reject.

    ``alias_rows`` are proposed aliases with ``alias_id``, ``product_id``,
    ``normalized_name`` and ``product_name`` (lower-cased canonical name of
    the product they point at, or None). Only pairs the draft names are
    touched; anything else proposed for the same string is left alone.
    Returns the groups, the rejects, and the draft pairs no alias matched.
    """
    groups: dict[int, list[int]] = {}
    rejects: list[int] = []
    seen: set = set()
    for row in alias_rows:
        pair = (row["normalized_name"], row.get("product_name"))
        if row.get("product_id") is not None and pair in confirm:
            groups.setdefault(int(row["product_id"]), []).append(int(row["alias_id"]))
            seen.add(pair)
        elif row.get("product_id") is None and row["normalized_name"] in reject:
            rejects.append(int(row["alias_id"]))
    return groups, rejects, confirm - seen


def run(path: Path, reviewer: str, apply: bool) -> int:
    with db.get_connection() as conn:
        txn = _Txn(conn)
        unmapped = products.unmapped_names(limit=100000, only_current=False, database=txn)
        load = pp.load_labels(path, expected={r["product_name"] for r in unmapped})
        if load.errors:
            print(f"{len(load.errors)} problem(s); nothing written:")
            for name, problem in list(load.errors.items())[:25]:
                print(f"  {str(name)[:60]:<60} {problem}")
            conn.rollback()
            return 1
        print(f"{len(load.proposals)} proposal(s) from "
              f"{len({p.raw_example for p in load.proposals})} name(s)")

        stats = pp.persist_proposals(load.proposals, database=txn)
        print("persist:", stats)

        confirm, reject = target_pairs(load.proposals)
        norms = sorted({n for n, _ in confirm} | reject)
        with txn.get_cursor() as cursor:
            cursor.execute(
                """
                SELECT a.alias_id, a.product_id, a.normalized_name,
                       lower(p.canonical_name) AS product_name
                FROM product_aliases a
                LEFT JOIN products p ON p.product_id = a.product_id
                WHERE a.status = 'proposed' AND a.normalized_name = ANY(%(n)s)
                """,
                {"n": norms},
            )
            rows = [dict(r) for r in cursor.fetchall()]
        groups, rejects, unmatched = plan_decisions(rows, confirm, reject)
        if unmatched:
            print(f"{len(unmatched)} draft pair(s) matched no proposed alias "
                  f"(already confirmed earlier?): {sorted(unmatched)[:5]}")

        confirmed = sum(products.confirm_group(pid, ids, reviewer, database=txn)
                        for pid, ids in groups.items())
        rejected = products.reject_aliases(rejects, reviewer, database=txn)
        print(f"confirmed {confirmed} alias(es) on {len(groups)} product(s); "
              f"rejected {rejected}")

        with txn.get_cursor() as cursor:
            cursor.execute(
                f"""
                SELECT product_id, canonical_name, status, {", ".join(products.ATTRIBUTES)}
                FROM products WHERE product_id = ANY(%(ids)s)
                """,
                {"ids": list(groups)},
            )
            bad = [r for r in cursor.fetchall()
                   if r["status"] != "confirmed"
                   or any(r[a] is None for a in products.ATTRIBUTES)]
        if bad:
            print("REFUSING: confirmed products with NULL attributes or not confirmed:")
            for r in bad:
                print("  ", dict(r))
            conn.rollback()
            return 1

        print("reinfer:", products.reinfer_alias_prices(database=txn))
        with txn.get_cursor() as cursor:
            cursor.execute(
                """
                SELECT a.raw_example, lower(p.canonical_name) AS product
                FROM product_aliases a JOIN products p USING (product_id)
                WHERE a.status = 'confirmed' AND a.price_is_per_item IS NULL
                  AND a.normalized_name = ANY(%(n)s)
                ORDER BY 1, 2
                """,
                {"n": norms},
            )
            undecided = cursor.fetchall()
        if undecided:
            print(f"{len(undecided)} confirmed alias(es) still have undecided "
                  "pricing and will not link:")
            for r in undecided:
                print(f"   {r['raw_example'][:70]:<70} -> {r['product']}")
        print("link:", products.link_deals(database=txn))

        if apply:
            conn.commit()
            print("COMMITTED")
        else:
            conn.rollback()
            print("dry run: rolled back (re-run with --apply to commit)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("labelled", type=Path)
    parser.add_argument("--reviewer", required=True,
                        help="recorded as reviewed_by, e.g. claude-review-<date>")
    parser.add_argument("--apply", action="store_true",
                        help="commit (default is a dry run that rolls back)")
    args = parser.parse_args(argv)
    return run(args.labelled, args.reviewer.strip(), args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
