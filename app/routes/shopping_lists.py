"""
Shopping list endpoints
"""

from fastapi import APIRouter, HTTPException, Query, status
import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from app.models.schemas import (
    ShoppingListResponse,
    ErrorResponse
)
from app.services.user_service import UserService
from app.services.database import DatabaseService
from app.services.store_service import StoreService
from app.services.shopping_optimizer import optimize_shopping_list, stores_from_items

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shopping-list", tags=["shopping-lists"])

db_service = DatabaseService()


def _resolve_generation(user_id: int, generation_id: Optional[UUID]) -> str:
    """The generation a list is built from: the one asked for, else the
    user's latest. 404 when the user has no generation at all."""
    if generation_id is not None:
        return str(generation_id)
    latest = db_service.get_latest_generation_id(user_id)
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No meal plan found for this user. Generate a meal plan first."
        )
    return latest


def _stored_list_response(stored: dict) -> ShoppingListResponse:
    return ShoppingListResponse(
        list_id=stored["list_id"],
        user_id=stored["user_id"],
        recipe_ids=stored["recipe_ids"],
        items=stored["items"],
        total_cost=stored["total_cost"],
        estimated_savings=stored["estimated_savings"],
        stores=stores_from_items(stored["items"]),
        created_at=stored["created_at"],
        is_completed=stored["is_completed"],
        completed_at=stored.get("completed_at"),
        generation_id=stored.get("generation_id"),
    )


def _build_and_persist(user_id: int, user: dict, generation_id: str) -> ShoppingListResponse:
    """Build a fresh shopping list from ONE generation's recipes and persist it.

    A list is one meal plan: it used to be built from the user's last 10
    recipes, which mixed plans whenever a generation was smaller than 10 or
    the user had generated twice. Raises 404 if that generation has no
    recipes for this user (unknown, or someone else's). Shared by GET's lazy
    path and the explicit POST /generate action.
    """
    recipes = db_service.get_generation_recipes(user_id, generation_id)
    if not recipes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No recipes found for meal plan {generation_id} of this user."
        )

    # DIETARY GATE (app/services/deal_gate.py). The optimizer resolves each
    # ingredient's deal citation against this deal set, so a restricted user
    # is only ever priced against deals whose confirmed products clear their
    # restrictions; a cited deal that was gated out becomes an unpriced line.
    deals = StoreService.get_current_deals_by_postal_code(
        user["postal_code"],
        dietary_restrictions=user.get("dietary_restrictions"),
    )
    result = optimize_shopping_list(recipes, deals)

    # No known regular prices -> no regular total either (NULL, not total).
    regular_total = (
        None if result.estimated_savings is None
        else result.total_cost + result.estimated_savings
    )
    recipe_ids = [recipe["recipe_id"] for recipe in recipes]

    list_id = db_service.save_shopping_list(
        user_id=user_id,
        recipe_ids=recipe_ids,
        items=result.items,
        total_cost=result.total_cost,
        estimated_savings=result.estimated_savings,
        regular_total=regular_total,
        generation_id=generation_id,
    )

    return ShoppingListResponse(
        list_id=list_id,
        user_id=user_id,
        recipe_ids=recipe_ids,
        items=result.items,
        total_cost=result.total_cost,
        estimated_savings=result.estimated_savings,
        stores=result.stores,
        created_at=datetime.now(),
        is_completed=False,
        generation_id=generation_id,
    )


_GENERATION_QUERY = Query(
    None,
    description="The meal plan (generation) to build the list from, as "
                "returned in the recipe-generation result. Omit for the "
                "user's latest generation.",
)


@router.get("/{user_id}",
            response_model=ShoppingListResponse,
            responses={
                404: {"model": ErrorResponse, "description": "Shopping list not found"},
                500: {"model": ErrorResponse, "description": "Internal server error"}
            })
async def get_shopping_list(
    user_id: int,
    generation_id: Optional[UUID] = _GENERATION_QUERY,
) -> ShoppingListResponse:
    """
    Get the shopping list for one of the user's meal plans.

    The list belongs to one generation: `generation_id` if given, otherwise
    the user's latest generation. Returns the latest persisted list for that
    generation, building (and persisting) one only if it has none yet. Does
    NOT write a new row on repeat calls — use POST /{user_id}/generate to
    explicitly regenerate.

    - **user_id**: User ID
    - **generation_id** (query, optional): the meal plan

    **Includes:**
    - Consolidated ingredients across that plan's recipes
    - Store assignments for each item (the cited deal's store)
    - Total cost and estimated savings
    """
    try:
        logger.info(f"Fetching shopping list for user {user_id}")

        user = UserService.get_user_by_id(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found"
            )

        if generation_id is None:
            latest_generation = db_service.get_latest_generation_id(user_id)
            if latest_generation is None:
                # Nothing generated since generations were recorded: serve the
                # user's last stored list (legacy, cross-plan) if there is one.
                legacy = db_service.get_latest_shopping_list(user_id)
                if legacy is not None:
                    return _stored_list_response(legacy)
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="No meal plan found for this user. Generate a meal plan first."
                )
            target = latest_generation
        else:
            target = str(generation_id)

        stored = db_service.get_latest_shopping_list(user_id, generation_id=target)
        if stored is not None:
            return _stored_list_response(stored)

        return _build_and_persist(user_id, user, target)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching shopping list for user {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch shopping list"
        )


@router.post("/{user_id}/generate",
             response_model=ShoppingListResponse,
             status_code=status.HTTP_201_CREATED,
             responses={
                 404: {"model": ErrorResponse, "description": "Shopping list not found"},
                 500: {"model": ErrorResponse, "description": "Internal server error"}
             })
async def generate_shopping_list(
    user_id: int,
    generation_id: Optional[UUID] = _GENERATION_QUERY,
) -> ShoppingListResponse:
    """
    Regenerate the shopping list for one of the user's meal plans.

    Always builds a brand-new consolidated list from that generation's
    recipes (`generation_id`, else the user's latest generation) and persists
    it as a new row, regardless of whether a list already exists. This is the
    explicit "regenerate my list" action.

    - **user_id**: User ID
    - **generation_id** (query, optional): the meal plan
    """
    try:
        logger.info(f"Generating a new shopping list for user {user_id}")

        user = UserService.get_user_by_id(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found"
            )

        return _build_and_persist(user_id, user, _resolve_generation(user_id, generation_id))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating shopping list for user {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate shopping list"
        )


@router.post("/{user_id}/mark-complete",
             status_code=status.HTTP_204_NO_CONTENT,
             responses={
                 404: {"model": ErrorResponse, "description": "Shopping list not found"}
             })
async def mark_shopping_list_complete(
    user_id: int,
    list_id: Optional[int] = Query(
        None,
        description="The list that was bought. Omit for the legacy behaviour "
                    "(the user's most recent list), which is ambiguous and "
                    "deprecated.",
    ),
):
    """
    Mark a shopping list as bought. **This is the spend-recording event.**

    Completing a list stamps `completed_at` and makes its `total_cost` count
    against the user's budget for the period containing that instant
    (`GET /users/{user_id}/budget`). A list that is never completed is a plan,
    not spend.

    - **list_id** (query, optional): the list to complete. Always prefer it.
      404 if no such list belongs to this user — a list_id is never honoured
      across users, since that would charge someone else's budget.
    - Omitting `list_id` keeps the original behaviour, the user's **most
      recent** list, unchanged for existing clients. It is deprecated: "most
      recent" moves if the client regenerates between shopping and reporting,
      so the wrong purchase can be recorded. Nothing in this repo's frontend
      calls it either way (verified), so the compatibility path exists for
      external callers only.

    Re-marking a list keeps its first `completed_at`: a second call is not a
    second purchase. Still 204 with no body, as before.
    """
    success = db_service.mark_shopping_list_complete(user_id, list_id)

    if not success:
        detail = (
            f"No shopping list {list_id} found for user {user_id}"
            if list_id is not None
            else f"No shopping list found for user {user_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail
        )

    return None
