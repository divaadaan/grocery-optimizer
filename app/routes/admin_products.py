"""Admin review UI for product proposals.

A browser front-end for the same decisions ``scripts/review_products.py``
makes. Both call into ``app/services/products.py``, so there is exactly one
definition of what confirming means -- a second implementation is how one
surface ends up promoting a product the other would have withheld.

Deliberately NOT under ``settings.api_prefix`` and not in the React app. This
is an internal tool, not a user feature: keeping it out of the versioned API
means it is obvious that no client depends on these shapes, and keeping it out
of the frontend bundle means no npm build step stands between the owner and
reviewing a queue.

SECURITY: this app has no authentication of any kind (see ROADMAP debt), so
these endpoints are unauthenticated like every other route -- and unlike the
others, they mutate allergen labels. That is acceptable only because the app
is run locally against a dev database. **Do not expose this host publicly
without putting auth in front of these routes first**, and note that a
reviewer identity is accepted from the client rather than verified, so
``reviewed_by`` is a convenience field, not an audit trail.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services import products

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

PAGE = Path(__file__).resolve().parents[1] / "static" / "review.html"


class Decision(BaseModel):
    """One review decision, covering a product and the aliases under it."""

    product_id: Optional[int] = None
    alias_ids: list[int] = Field(default_factory=list)
    action: Literal["confirm", "reject"]
    # Attribute overrides applied before confirming. Only products.ATTRIBUTES
    # are accepted; anything else is a 400, not a silent no-op. An attribute
    # the reviewer left undecided is omitted, not sent as false.
    attributes: Optional[dict[str, bool]] = None
    reviewer: Optional[str] = None


class Fill(BaseModel):
    """Decide NULL attributes on an already-confirmed product."""

    product_id: int
    attributes: dict[str, bool]
    reviewer: Optional[str] = None


class DecisionResult(BaseModel):
    decided: int
    product_rejected: bool = False
    attributes: dict[str, bool] = Field(default_factory=dict)
    stats: dict[str, int] = Field(default_factory=dict)


def _reviewer(supplied: str | None) -> str:
    return (supplied or os.environ.get("REVIEWER") or "web").strip()[:80] or "web"


@router.get("/review", include_in_schema=False)
async def review_page() -> FileResponse:
    """The single-page review UI."""
    if not PAGE.is_file():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"review.html is missing at {PAGE}",
        )
    # no-store: the queue changes with every decision, and a cached page
    # showing already-decided items invites double-reviewing.
    return FileResponse(PAGE, media_type="text/html",
                        headers={"Cache-Control": "no-store"})


@router.get("/products/queue")
async def queue(limit: int = 5000) -> dict[str, Any]:
    """The pending review queue, grouped by product."""
    rows = products.pending_proposals(limit=limit)
    groups, orphans = products.group_proposals(rows)
    return {
        "groups": groups,
        "orphans": orphans,
        # Confirmed products withheld by a NULL attribute (migration 015).
        # Not proposals, so they would otherwise never be shown again.
        "undecided": products.undecided_products(),
        "attributes": list(products.ATTRIBUTES),
        "rules": dict(products.ATTRIBUTE_RULES),
        "labels": list(products.REVIEW_LABELS),
        "stats": products.review_stats(),
    }


@router.post("/products/fill")
async def fill(body: Fill) -> dict[str, Any]:
    """Set NULL attributes on a confirmed product. Never overwrites a value."""
    reviewer = _reviewer(body.reviewer)
    if not body.attributes:
        raise HTTPException(status_code=400, detail="no attributes given")
    try:
        values = products.fill_undecided_attributes(body.product_id, body.attributes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not values:
        raise HTTPException(status_code=404, detail="no such product")
    logger.info("Review: %r filled %s on product %s",
                reviewer, body.attributes, body.product_id)
    return {"product_id": body.product_id, "attributes": values,
            "clears": products.clears_labels(
                {"product_id": body.product_id, **values})}


@router.post("/products/decide", response_model=DecisionResult)
async def decide(decision: Decision) -> DecisionResult:
    """Confirm or reject a group of aliases.

    Confirming requires a ``product_id``: an alias with no product cannot be
    confirmed, because there would be nothing to read attributes from. That is
    the fail-closed rule, enforced here as well as in the CLI.
    """
    reviewer = _reviewer(decision.reviewer)

    if not decision.alias_ids:
        raise HTTPException(status_code=400, detail="no alias_ids given")

    if decision.action == "reject":
        decided = products.reject_aliases(decision.alias_ids, reviewer)
        rejected_product = False
        if decision.product_id is not None:
            rejected_product = products.reject_product_if_orphaned(
                decision.product_id, reviewer
            )
        return DecisionResult(
            decided=decided,
            product_rejected=rejected_product,
            stats=products.review_stats(),
        )

    if decision.product_id is None:
        raise HTTPException(
            status_code=400,
            detail="cannot confirm an alias with no product - reject it instead",
        )

    applied: dict[str, bool] = {}
    if decision.attributes:
        try:
            applied = products.set_attributes(
                decision.product_id, decision.attributes
            )
        except products.AttributeConflict as exc:
            # Raised before any write, and before confirm_group below, so the
            # alias stays proposed and no attribute changed.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    decided = products.confirm_group(
        decision.product_id, decision.alias_ids, reviewer
    )
    logger.info(
        "Review: confirmed product %s and %d alias(es) as %r%s",
        decision.product_id, decided, reviewer,
        f" (attributes {applied})" if applied else "",
    )
    return DecisionResult(
        decided=decided, attributes=applied, stats=products.review_stats()
    )


@router.post("/products/link")
async def link() -> dict[str, Any]:
    """Run the deterministic link step and report what it attached."""
    stats = products.link_deals()
    return {"link": stats, "stats": products.review_stats()}
