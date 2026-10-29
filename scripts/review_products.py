#!/usr/bin/env python
"""Review product proposals from the terminal, and run the link step.

The pipeline, and where this sits in it::

    ingest  ->  link  ->  propose  ->  REVIEW  ->  link
    (flipp)     (det.)    (claude)    (you)       (det.)

Nothing an LLM proposed affects a single user until it passes through here.
``link`` only ever follows ``status='confirmed'`` rows, so the review step is
the gate, not a formality.

There is also a browser UI at ``/admin/review`` (``app/routes/admin_products.py``)
which is usually the nicer way to work a queue. **Both call the same functions
in ``app/services/products.py``** -- no grouping, confirming or rejecting logic
lives in this file. A second implementation of "what does confirming mean" is
how one surface ends up promoting a product the other would have withheld.

Review is grouped by product, not by alias
------------------------------------------
One product arrives under many flyer strings: 91 ingested names resolved to 21
products on the first pass, so reviewing alias-by-alias meant 88 decisions to
establish 21 facts. That scales the wrong way -- the full staples list projects
to ~2,000 names, which alias-by-alias is a data-entry job nobody finishes, and
an abandoned review leaves deals withheld.

A decision is made once per product and applies to every string mapping to it.
This is safe to batch precisely because the dietary judgement is a property of
the product: confirming "milk contains dairy" once is not a weaker statement
than confirming it fifteen times. Per-string override is still there (``p``).

Why there is no ``--auto-confirm``
----------------------------------
Auto-confirming above a confidence threshold would hand the allergen decision
back to the model, and model confidence is not calibrated to allergen risk.
Grouping is the right way to make review cheap; skipping review is not.

Usage::

    python scripts/review_products.py --list
    python scripts/review_products.py --review
    python scripts/review_products.py --link
    python scripts/review_products.py --unmapped
    python scripts/review_products.py --reinfer-prices
    python scripts/review_products.py --fill-undecided
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import products  # noqa: E402

ATTR_LABEL = {
    "contains_meat": "meat",
    "contains_fish": "fish/shellfish",
    "contains_dairy": "dairy",
    "contains_egg": "egg",
    "contains_nuts": "nuts",
    "contains_animal_byproduct": "animal by-product",
    "contains_gluten": "gluten",
    "not_kosher": "not kosher",
}


def _phrase(attr: str) -> str:
    """The yes/no question an attribute asks: "contains meat", but
    "is not kosher" -- not_kosher is not a "contains" fact, and "contains
    not kosher" reads as nonsense to a reviewer at a prompt."""
    if attr.startswith("contains_"):
        return f"contains {ATTR_LABEL[attr]}"
    return f"is {ATTR_LABEL[attr]}"

#: Shown wherever a reviewer decides contains_animal_byproduct. The split is
#: easy to get backwards, and backwards serves gelatin to a vegetarian.
BYPRODUCT_RULE = (
    "animal by-product = honey, beeswax, carmine/cochineal, shellac/glaze, "
    "bone-char sugar, lanolin vitamin D3.\n"
    "  Gelatin, lard, tallow and animal rennet are MEAT, not by-product.")

#: Shown wherever a reviewer decides contains_gluten.
GLUTEN_RULE = (
    "gluten = wheat (spelt, kamut, durum, semolina, farro), barley, rye, "
    "triticale, malt, brewer's yeast.\n"
    "  Oats are YES unless labelled certified gluten-free; 'may contain "
    "gluten' is YES. Judge the product as sold in Canadian flyers.")

#: Shown wherever a reviewer decides not_kosher. The text is
#: products.ATTRIBUTE_RULES' own, so the terminal and the proposer agree.
KOSHER_RULE = "not kosher = " + products.ATTRIBUTE_RULES["not_kosher"] + "."

#: How many of a group's aliases to print before truncating.
ALIAS_PREVIEW = 6


def _reviewer() -> str:
    return os.environ.get("REVIEWER") or os.environ.get("USER") or "unknown"


def _queue() -> tuple[list[dict], list[dict]]:
    return products.group_proposals(products.pending_proposals())


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
    # NULL is "nobody decided", which fails closed -- never "absent".
    undecided = [ATTR_LABEL[a] for a in products.ATTRIBUTES if row.get(a) is None]
    summary = ", ".join(present) if present else (
        "no animal products or nuts" if not undecided else "nothing known")
    if undecided:
        summary += f"; UNDECIDED: {', '.join(undecided)}"
    return summary


def _clears(row: dict) -> str:
    if row.get("product_id") is None:
        return "nothing - withheld"
    labels = products.clears_labels(row)
    return ", ".join(labels) if labels else "none of them"


def cmd_list() -> int:
    groups, orphans = _queue()
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
        print(f"  {group['canonical_name']}  [{group['category']}]"
              f"  -- {len(group['aliases'])} alias(es), seen {group['times_seen']}x")
        print(f"      contains: {_attr_summary(group)}")
        print(f"      clears:   {_clears(group)}")

    if orphans:
        print(f"\n  (unplaceable: {len(orphans)} string(s) with no product -- "
              f"multi-product flyer copy and garbled text)")
    print("\nRun with --review to decide, or open /admin/review in a browser.")
    return 0


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
  e = EDIT the attributes    -> flip meat/fish/dairy/egg/nuts/animal
                                by-product/gluten/not-kosher, then decide
  p = PICK strings one by one-> for a group where one string does not belong
  s = SKIP, decide later     -> no decision; stays in the queue
  q = QUIT                   -> stops here, keeps what you decided

Nothing reaches a user until you confirm it. When unsure, skip or reject --
a withheld deal costs one offer, a wrong label costs more.

""" + BYPRODUCT_RULE + "\n  " + GLUTEN_RULE + "\n  " + KOSHER_RULE + "\n"

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
    print("-" * 72)
    print(f"  product    {group['canonical_name']}  [{group['category']}]"
          f"  ({group['product_status']})")
    print(f"  contains   {_attr_summary(group)}")
    print(f"  clears     {_clears(group)}")
    if group["is_pantry_staple"]:
        print("  pantry     yes")
    print(f"  strings    {len(aliases)} mapping to it, "
          f"seen {group['times_seen']}x total")
    for alias in aliases[:ALIAS_PREVIEW]:
        conf = alias["confidence"]
        conf_s = f"{float(conf):.2f}" if conf is not None else " n/a"
        print(f"      x{alias['times_seen']:<2} conf {conf_s}  "
              f"{alias['raw_example'][:56]}")
        # A multi-product tile: this one string is shared across several
        # product groups. Saying so matters -- otherwise the whole tile text
        # shows up under one product and looks like the split never happened.
        siblings = [
            n for n in (alias.get("tile_products") or [])
            if n != group["canonical_name"]
        ]
        if siblings:
            total = len(alias["tile_products"])
            print(f"          part of a {total}-product tile; the rest is "
                  f"handled by: {', '.join(siblings)}")
            if alias["price_is_per_item"] is False:
                print("          (bundle price - not this item's own price)")
            elif alias["price_is_per_item"] is None:
                print("          (pricing undecided - stays withheld)")
    if len(aliases) > ALIAS_PREVIEW:
        print(f"      ... and {len(aliases) - ALIAS_PREVIEW} more")

    reasons = {a["proposal_reason"] for a in aliases if a["proposal_reason"]}
    if reasons:
        print(f"  reason     {sorted(reasons)[0]}")


def _edit_attributes(group: dict) -> bool:
    """Flip attributes on a proposed product. True if anything changed."""
    # Explicit mapping rather than zipping letters against the attribute
    # tuple: the pairing has to stay obvious to whoever reads this next.
    keys = {
        "m": "contains_meat",
        "f": "contains_fish",
        "d": "contains_dairy",
        "e": "contains_egg",
        "n": "contains_nuts",
        "a": "contains_animal_byproduct",
        "g": "contains_gluten",
        "k": "not_kosher",
    }
    print("  Current labels:")
    for letter, attr in keys.items():
        value = group.get(attr)
        state = "???" if value is None else ("YES" if value else "no ")
        print(f"    {letter} = {_phrase(attr):<27} {state}")
    print(f"  {BYPRODUCT_RULE}")
    print(f"  {GLUTEN_RULE}")
    print(f"  {KOSHER_RULE}")
    print("  Type the letters you want to FLIP, e.g. 'dn' flips dairy and nuts.")
    print("  An undecided (???) label is not flipped; you are asked for it.")
    print("  Press Enter alone to leave them as they are.")
    raw = input("  flip> ").strip().lower()

    flips = {keys[c] for c in raw if c in keys}
    unknown = {c for c in raw if c not in keys and not c.isspace()}
    if unknown:
        print(f"  ignored unrecognised letter(s): {''.join(sorted(unknown))}")
    if not flips:
        print("  unchanged")
        return False

    updates = {}
    for attr in flips:
        if group.get(attr) is None:
            # Flipping an unknown has no meaning; "not None" would read as
            # True by accident. Ask instead.
            answer = _ask(f"  {_phrase(attr)}? [y/n/s]> ", "yns")
            if answer != "s":
                updates[attr] = answer == "y"
        else:
            updates[attr] = not bool(group[attr])
    if not updates:
        print("  unchanged")
        return False
    try:
        products.set_attributes(group["product_id"], updates)
    except products.AttributeConflict as exc:
        print(f"  REFUSED: {exc}")
        return False
    group.update(updates)
    for attr, value in updates.items():
        print(f"  {ATTR_LABEL[attr]} -> {value}")
    return True


def _pick_individually(group: dict, reviewer: str) -> int:
    """Per-string decisions within one product group."""
    confirm_ids: list[int] = []
    reject_ids: list[int] = []
    print(f"  Deciding {len(group['aliases'])} string(s) one at a time.")
    for alias in group["aliases"]:
        print(f"    {alias['raw_example'][:64]}")
        answer = _ask(f"      is this {group['canonical_name']}? [y/n/s]> ", "yns")
        if answer == "y":
            confirm_ids.append(alias["alias_id"])
        elif answer == "n":
            reject_ids.append(alias["alias_id"])

    decided = 0
    if confirm_ids:
        decided += products.confirm_group(group["product_id"], confirm_ids, reviewer)
        print(f"    confirmed {len(confirm_ids)}")
    if reject_ids:
        decided += products.reject_aliases(reject_ids, reviewer)
        print(f"    rejected {len(reject_ids)}")
    if not confirm_ids:
        products.reject_product_if_orphaned(group["product_id"], reviewer)
    return decided


def cmd_review() -> int:
    groups, orphans = _queue()
    if not groups and not orphans:
        print("No pending proposals.")
        return 0

    reviewer = _reviewer()
    total = sum(len(g["aliases"]) for g in groups) + len(orphans)
    print(f"Reviewing {len(groups)} product group(s) covering {total} "
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
            _edit_attributes(group)
            print(f"  now clears: {_clears(group)}")
            if _ask("  confirm this product and all its strings? [y/n]> ",
                    "yn") != "y":
                continue
            answer = "y"

        ids = [a["alias_id"] for a in group["aliases"]]
        if answer == "y":
            decided += products.confirm_group(group["product_id"], ids, reviewer)
            print(f"  confirmed {group['canonical_name']} and {len(ids)} string(s)")
        else:
            decided += products.reject_aliases(ids, reviewer)
            products.reject_product_if_orphaned(group["product_id"], reviewer)
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
        if _ask("  reject all of them? yes / skip [y/s]> ", "ys") == "y":
            decided += products.reject_aliases(
                [a["alias_id"] for a in orphans], reviewer
            )
            print(f"  rejected {len(orphans)}")

    print("-" * 72)
    print(f"Decided {decided} string(s).")
    if decided:
        print("Run --link to apply the new mappings to existing deals.")
    return 0


def cmd_fill_undecided() -> int:
    """Decide NULL attributes on products that are already confirmed.

    Migrations 015, 017 and 021 added contains_animal_byproduct,
    contains_gluten and not_kosher with no value for products reviewed before they existed. NULL fails closed, so those products are
    withheld from vegans -- and being confirmed, they never re-enter the
    proposal queue. This walks the ones whose NULL actually decides a verdict.
    Writes only fill NULLs (``products.fill_undecided_attributes``), so nothing
    a reviewer already decided can be changed from here.
    """
    pending = products.undecided_products()
    if not pending:
        print("No confirmed product has an undecided attribute that matters.")
        return 0
    print(f"{len(pending)} confirmed product(s) have an undecided attribute that "
          f"is withholding them from someone.\n")
    print(f"  {BYPRODUCT_RULE}")
    print(f"  {GLUTEN_RULE}")
    print(f"  {KOSHER_RULE}\n")
    print("  y = yes, it does   n = no, it does not   s = skip   q = quit\n")

    filled = 0
    for product in pending:
        print("-" * 72)
        print(f"  product    {product['canonical_name']}  [{product['category']}]")
        print(f"  contains   {_attr_summary(product)}")
        print(f"  clears     {_clears(product)}")
        print(f"  a 'no' would also clear: {', '.join(product['would_clear'])}")
        updates: dict[str, bool] = {}
        for attr in product["undecided"]:
            answer = _ask(f"  {_phrase(attr)}? [y/n/s/q]> ", "ynsq")
            if answer == "q":
                print(f"Filled {filled} attribute(s).")
                return 0
            if answer in "yn":
                updates[attr] = answer == "y"
        if updates:
            products.fill_undecided_attributes(product["product_id"], updates)
            filled += len(updates)
            saved = ", ".join(f"{ATTR_LABEL[a]}={v}" for a, v in updates.items())
            print(f"  saved {saved}")
    print("-" * 72)
    print(f"Filled {filled} attribute(s).")
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


def cmd_reinfer() -> int:
    """Fill undecided tile pricing from the deal text, after an ingestion run."""
    stats = products.reinfer_alias_prices()
    print(f"  aliases examined   {stats['examined']}")
    print(f"  decided per-item   {stats['filled_true']}")
    print(f"  decided bundle     {stats['filled_false']}")
    print(f"  still undecided    {stats['still_null']}")
    if stats["still_null"]:
        print("\nStill-undecided aliases stay withheld: the provider sent no")
        print("unit, no sale_story and no pre_price_text for those tiles, so")
        print("there is nothing to infer from. Decide them by hand in review,")
        print("or leave them withheld.")
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
    group.add_argument("--reinfer-prices", dest="reinfer", action="store_true",
                       help="fill undecided tile pricing from the deal text")
    group.add_argument("--fill-undecided", dest="fill_undecided",
                       action="store_true",
                       help="decide NULL attributes (e.g. animal by-product) "
                            "on already-confirmed products")
    args = parser.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.review:
        return cmd_review()
    if args.fill_undecided:
        return cmd_fill_undecided()
    if args.link:
        return cmd_link()
    if args.unmapped:
        return cmd_unmapped()
    return cmd_reinfer()


if __name__ == "__main__":
    raise SystemExit(main())
