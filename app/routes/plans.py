"""Approach C, step 1: propose this week's trays for the user to confirm.

``POST /plans/trays`` returns the ANCHORS / SIDES / STOCK-UP trays built in
code from the user's **gated** deals (``app/services/trays.py``), plus the
pantry staples to confirm. Nothing is persisted: the trays are cheap and
deterministic, and generation rebuilds them to check what the client sends
back. Design: ``experiments/chef/DESIGN_APPROACH_C.md`` §5.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from app.models.schemas import ErrorResponse, TrayProposalRequest, TrayProposalResponse
from app.services import deal_labeller, products, recipe_catalog, trays
from app.services.database import DatabaseService
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/plans", tags=["plans"])


def propose_for_user(user: dict) -> trays.TrayProposal:
    """The production tray proposal for a stored user profile."""
    return trays.propose_trays(
        user["postal_code"], user.get("dietary_restrictions") or [],
        fetch_deals=DatabaseService().fetch_gated_deals,
        fetch_labels=deal_labeller.fetch_labels,
        fetch_pantry=recipe_catalog.fetch_pantry_staples,
    )


def proposal_warnings(proposal: trays.TrayProposal, num_meals: int) -> list[str]:
    warnings = []
    counts = proposal.counts
    if counts.get("gated_deals", 0) == 0:
        warnings.append("No current deals for this postal code (or none clear your "
                        "dietary restrictions).")
    if counts.get("unlabelled"):
        warnings.append(f"{counts['unlabelled']} deal(s) are not labelled yet and are not "
                        "in the trays.")
    if not proposal.anchors:
        warnings.append("No protein deals this week: there is nothing to build a dish around.")
    elif len(proposal.anchors) < num_meals:
        warnings.append(f"Only {len(proposal.anchors)} anchor(s) for {num_meals} meals: some "
                        "anchors will serve more than one dish.")
    return warnings


@router.post("/trays",
             response_model=TrayProposalResponse,
             responses={
                 404: {"model": ErrorResponse, "description": "User not found"},
                 422: {"model": ErrorResponse,
                       "description": "Profile carries an unsupported dietary restriction"},
                 500: {"model": ErrorResponse, "description": "Internal server error"},
             })
async def propose_trays(request: TrayProposalRequest) -> TrayProposalResponse:
    """This week's trays for the user to keep or reject, and their pantry to confirm.

    - **anchors**: protein deals, each the cheapest for its item across chains
    - **sides**: vegetables, starches, fruit and dairy
    - **stock_up**: discounted pantry staples sold by at least two chains
    - **pantry**: on-hand staples that clear the user's restrictions

    Only deals that pass the dietary gate are considered.
    """
    user = UserService.get_user_by_id(request.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"User with ID {request.user_id} not found")
    unsupported = products.unsupported_restrictions(user.get("dietary_restrictions"))
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(products.UnsupportedRestrictionError(unsupported))
            + " Update the profile to remove them, then try again.")
    if not user.get("postal_code"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="The profile has no postal code.")
    try:
        proposal = propose_for_user(user)
    except Exception as e:
        logger.error("Tray proposal failed for user %s: %s", request.user_id, e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to build this week's trays") from e
    body = proposal.as_dict()
    return TrayProposalResponse(
        user_id=request.user_id, postal_code=user["postal_code"], num_meals=request.num_meals,
        warnings=proposal_warnings(proposal, request.num_meals), **body)
