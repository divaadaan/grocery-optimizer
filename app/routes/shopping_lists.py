"""
Shopping list endpoints
"""

from fastapi import APIRouter, HTTPException, status
import logging
from datetime import datetime

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


def _build_and_persist(user_id: int, user: dict) -> ShoppingListResponse:
    """Build a fresh shopping list from the user's current recipes and persist it.

    Raises 404 if the user has no recipes yet. Shared by GET's lazy
    first-time path and the explicit POST /generate action.
    """
    recipes = db_service.get_user_recipes(user_id)
    if not recipes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No recipes found for this user. Generate a meal plan first."
        )

    deals = StoreService.get_current_deals_by_postal_code(user["postal_code"])
    result = optimize_shopping_list(recipes, deals)

    regular_total = result.total_cost + result.estimated_savings
    recipe_ids = [recipe["recipe_id"] for recipe in recipes]

    list_id = db_service.save_shopping_list(
        user_id=user_id,
        recipe_ids=recipe_ids,
        items=result.items,
        total_cost=result.total_cost,
        estimated_savings=result.estimated_savings,
        regular_total=regular_total
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
        is_completed=False
    )


@router.get("/{user_id}",
            response_model=ShoppingListResponse,
            responses={
                404: {"model": ErrorResponse, "description": "Shopping list not found"},
                500: {"model": ErrorResponse, "description": "Internal server error"}
            })
async def get_shopping_list(user_id: int) -> ShoppingListResponse:
    """
    Get the most recent shopping list for a user.

    Returns the user's latest persisted shopping list, building one only
    if they don't have one yet (lazy first-time generation). Does NOT
    write a new row on repeat calls — use POST /{user_id}/generate to
    explicitly regenerate.

    - **user_id**: User ID

    **Includes:**
    - Consolidated ingredients across all recipes
    - Store assignments for each item (best price)
    - Total cost and estimated savings
    - Shopping route optimization (future)
    """
    try:
        logger.info(f"Fetching shopping list for user {user_id}")

        user = UserService.get_user_by_id(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found"
            )

        latest = db_service.get_latest_shopping_list(user_id)
        if latest is not None:
            return ShoppingListResponse(
                list_id=latest["list_id"],
                user_id=latest["user_id"],
                recipe_ids=latest["recipe_ids"],
                items=latest["items"],
                total_cost=latest["total_cost"],
                estimated_savings=latest["estimated_savings"],
                stores=stores_from_items(latest["items"]),
                created_at=latest["created_at"],
                is_completed=latest["is_completed"]
            )

        return _build_and_persist(user_id, user)

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
async def generate_shopping_list(user_id: int) -> ShoppingListResponse:
    """
    Regenerate the shopping list for a user from their current recipes.

    Always builds a brand-new consolidated list and persists it as a new
    row, regardless of whether a list already exists. This is the explicit
    "regenerate my list" action.

    - **user_id**: User ID
    """
    try:
        logger.info(f"Generating a new shopping list for user {user_id}")

        user = UserService.get_user_by_id(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found"
            )

        return _build_and_persist(user_id, user)

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
async def mark_shopping_list_complete(user_id: int):
    """
    Mark a shopping list as completed.

    Updates the shopping list status when user finishes shopping.
    """
    success = db_service.mark_shopping_list_complete(user_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No shopping list found for user {user_id}"
        )

    return None
