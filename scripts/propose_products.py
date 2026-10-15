#!/usr/bin/env python
"""Export unmapped deal names for labelling, and import the labels back.

No API key, no SDK, no network egress. The labelling is done by whichever
Claude session is working in this repo -- it can read
``products.RESTRICTION_ATTRIBUTES``, ``seed_catalog.py`` and the existing
catalogue for context, which an outbound
API call handed a prompt through a pinhole cannot.

Three steps::

    python scripts/propose_products.py --export /tmp/unmapped.json
    # ask a Claude session: "label the items in /tmp/unmapped.json per its
    # instructions, write /tmp/labelled.json"
    python scripts/propose_products.py --import /tmp/labelled.json

Everything imported lands as ``status='proposed'`` and affects no user. A human
confirms with ``scripts/review_products.py --review``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.database import db  # noqa: E402
from app.services import product_proposer as pp  # noqa: E402
from app.services import products  # noqa: E402


def cmd_export(path: Path, limit: int, include_expired: bool) -> int:
    unmapped = products.unmapped_names(
        limit=limit, only_current=not include_expired, database=db
    )
    names = [row["product_name"] for row in unmapped]
    if not names:
        print("Nothing unmapped. Run scripts/review_products.py --link first if "
              "you have just confirmed proposals.")
        return 0

    existing = products.confirmed_products(database=db)
    bundle = pp.export_bundle(names, existing)
    path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    print(f"Wrote {path}")
    print(f"  {len(names)} name(s) to label")
    print(f"  {len(existing)} confirmed product(s) to match against")
    print("\nNext: ask a Claude session to label it --")
    print(f'  "label the items in {path} following its instructions, '
          f'write <labelled>.json"')
    print("Then: python scripts/propose_products.py --import <labelled>.json")
    return 0


def cmd_import(path: Path, strict: bool) -> int:
    # The exported set is re-derived rather than trusted from the file, so a
    # label for a name that is no longer unmapped (or never was) is caught.
    unmapped = products.unmapped_names(limit=10000, only_current=False, database=db)
    expected = {row["product_name"] for row in unmapped}

    load = pp.load_labels(path, expected=expected if strict else None)

    if load.errors:
        print(f"{len(load.errors)} problem(s):")
        for name, problem in list(load.errors.items())[:25]:
            print(f"  {str(name)[:52]:<52} {problem}")
        if len(load.errors) > 25:
            print(f"  ... and {len(load.errors) - 25} more")

    if load.unlabelled:
        # Not a problem: labelling a subset is how a long queue gets worked
        # through. These simply stay withheld until someone labels them.
        print(f"\n{len(load.unlabelled)} unmapped name(s) still have no label "
              f"(they stay withheld):")
        for name in load.unlabelled[:5]:
            print(f"  {name[:60]}")
        if len(load.unlabelled) > 5:
            print(f"  ... and {len(load.unlabelled) - 5} more")

    if not load.proposals:
        print("\nNothing valid to persist.")
        return 1

    print(f"\n{len(load.proposals)} valid label(s).")
    if load.errors and strict:
        print("Refusing to persist while any entry is malformed (--strict). Fix the")
        print("file, or re-run with --no-strict to persist only the valid entries.")
        return 1

    stats = pp.persist_proposals(load.proposals, database=db)
    print(f"  new aliases        {stats['aliases_new']}")
    print(f"  re-seen aliases    {stats['aliases_seen_again']}")
    print(f"  products proposed  {stats['products_proposed']}")
    print(f"  skipped            {stats['skipped']}")
    print("\nNothing is live yet. Review with:")
    print("    python scripts/review_products.py --review")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--export", metavar="FILE", help="write unmapped names to label")
    group.add_argument("--import", dest="import_", metavar="FILE",
                       help="validate and persist labels as proposals")
    parser.add_argument("--limit", type=int, default=100,
                        help="how many names to export (default 100)")
    parser.add_argument("--include-expired", action="store_true",
                        help="also export names whose deals have all expired")
    parser.add_argument("--no-strict", dest="strict", action="store_false",
                        help="persist valid entries even if some are bad")
    args = parser.parse_args(argv)

    if args.export:
        return cmd_export(Path(args.export), args.limit, args.include_expired)
    return cmd_import(Path(args.import_), args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
