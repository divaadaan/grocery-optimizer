#!/usr/bin/env python
"""Label current deals with {item, role, package size} for the tray builder.

Runs automatically after ingestion when ``DEAL_LABELLING_ENABLED`` is on; this
is the manual path (backfill, or after a PROMPT_VERSION bump). Labels only
current deals that have no label at the current prompt version, so a re-run
resumes where the last one stopped. Design:
``experiments/chef/DESIGN_APPROACH_C.md`` section 3.

    # how many deals are waiting, no model calls
    .venv/bin/python scripts/label_deals.py L4G3E8 --pending
    # label them (hf: models need HUGGINGFACE_API_KEY)
    .venv/bin/python scripts/label_deals.py L4G3E8
    # every postal code, a different model, at most 80 deals
    .venv/bin/python scripts/label_deals.py --model ollama:qwen2.5:7b --limit 80

Prerequisite: migration 024 (deal_labels) applied to DATABASE_URL.
Exits 1 when the run stopped on an infrastructure failure or left deals
unlabelled.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.services.deal_labeller import fetch_pending, label_pending  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("postal_code", nargs="?", default=None,
                        help="limit to one postal code (default: all)")
    parser.add_argument("--model", default=None,
                        help=f"model spec (default {settings.llm_labeller_model})")
    parser.add_argument("--limit", type=int, default=None, help="label at most N deals")
    parser.add_argument("--pending", action="store_true",
                        help="only count deals waiting for a label")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    postal_code = args.postal_code.replace(" ", "").upper() if args.postal_code else None

    if args.pending:
        print(f"{len(fetch_pending(postal_code))} deal(s) waiting for a label")
        return 0

    report = label_pending(postal_code, spec=args.model, limit=args.limit)
    print(json.dumps(report.as_dict(), indent=2))
    return 1 if (report.stopped_on_infrastructure or report.unlabelled) else 0


if __name__ == "__main__":
    raise SystemExit(main())
