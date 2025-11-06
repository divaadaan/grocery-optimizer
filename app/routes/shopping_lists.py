"""
Shopping list endpoints
"""

from fastapi import APIRouter, HTTPException, status
import logging

from app.models.schemas import (
    ShoppingListResponse,
    ErrorResponse
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shopping-list", tags=["shopping-lists"])


@router.get("/{user_id}",
            response_model=ShoppingListResponse,
            responses={
                404: {"model": ErrorResponse, "description": "Shopping list not found"},
                500: {"model": ErrorResponse, "description": "Internal server error"}
            })
async def get_shopping_list(user_id: int) -> ShoppingListResponse:
    """
    Get the most recent shopping list for a user.

    Returns a consolidated shopping list from approved recipes,
    organized by store for optimal shopping efficiency.

    - **user_id**: User ID

    **Includes:**
    - Consolidated ingredients across all recipes
    - Store assignments for each item (best price)
    - Total cost and estimated savings
    - Shopping route optimization (future)

    **Note:** This is currently a stub. Full implementation pending.
    """
    try:
        logger.info(f"Fetching shopping list for user {user_id}")

        # TODO: Implement shopping list retrieval
        # Will pull from shopping_lists table and format response

        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Shopping list generation not yet implemented. "
                   "This endpoint will consolidate ingredients from recipes, "
                   "assign items to stores with best prices, and calculate total costs."
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching shopping list for user {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch shopping list"
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
    # TODO: Implement mark complete functionality
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Mark shopping list complete not yet implemented"
    )
