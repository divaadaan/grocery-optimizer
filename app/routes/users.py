"""
User endpoints
"""

from fastapi import APIRouter, HTTPException, status
import logging

from app.models.schemas import (
    BudgetStatusResponse,
    DietaryRestrictionsResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
    ErrorResponse
)
from app.services import products
from app.services.budget import budget_status
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def _refuse_unsupported(restrictions) -> None:
    """422 for a restriction label no curated product attribute enforces.

    Storing one would promise the user a check that never happens: the gate
    would withhold everything unlinked and the recipe verdict would have
    nothing to decide. Refused here, before it is stored."""
    try:
        products.require_supported_restrictions(restrictions)
    except products.UnsupportedRestrictionError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )


@router.get("/dietary-restrictions",
            response_model=DietaryRestrictionsResponse)
async def get_dietary_restrictions() -> DietaryRestrictionsResponse:
    """
    The dietary restriction labels this app supports.

    Exactly the keys of `products.RESTRICTION_ATTRIBUTES`: each is enforced
    from curated product attributes (deals served, recipes judged). Any other
    label is refused with 422 by register / update.
    """
    return DietaryRestrictionsResponse(supported=products.supported_restrictions())


@router.post("/register",
             response_model=UserResponse,
             status_code=status.HTTP_201_CREATED,
             responses={
                 400: {"model": ErrorResponse, "description": "Invalid input or user already exists"},
                 422: {"model": ErrorResponse, "description": "Unsupported dietary restriction"},
                 500: {"model": ErrorResponse, "description": "Internal server error"}
             })
async def register_user(user_data: UserCreate) -> UserResponse:
    """
    Register a new user.

    Creates a new user account with dietary preferences and budget settings.

    - **email**: User's email address (must be unique)
    - **postal_code**: User's postal code for finding local deals
    - **budget**: Grocery spend limit for ONE budget period (default: $100.00).
      Not a per-meal-plan ceiling: spend accumulates across the period from
      completed shopping lists. See `GET /users/{user_id}/budget`.
    - **budget_period**: How long that period is — `"weekly"` (Monday-anchored
      ISO week) or `"monthly"` (calendar month from the 1st). Default
      `"monthly"`. Calendar-anchored, not rolling.
    - **household_size**: Number of people in household (default: 1)
    - **dietary_restrictions**: List of dietary restrictions (e.g., ["vegetarian", "gluten_free"]).
      Only labels from `GET /users/dietary-restrictions` are accepted; any
      other label is refused with 422.
    """
    _refuse_unsupported(user_data.dietary_restrictions)
    try:
        user = UserService.create_user(user_data)

        return UserResponse(
            user_id=user["user_id"],
            email=user["email"],
            postal_code=user["postal_code"],
            budget=user["budget"],
            budget_period=user.get("budget_period") or "monthly",
            household_size=user["household_size"],
            dietary_restrictions=user["dietary_restrictions"],
            created_at=user["created_at"],
            is_active=user["is_active"]
        )

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        logger.error(f"Error registering user: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to register user"
        )


@router.get("/{user_id}",
            response_model=UserResponse,
            responses={
                404: {"model": ErrorResponse, "description": "User not found"}
            })
async def get_user(user_id: int) -> UserResponse:
    """
    Get user by ID.

    Retrieve user information and preferences.
    """
    user = UserService.get_user_by_id(user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )

    return UserResponse(
        user_id=user["user_id"],
        email=user["email"],
        postal_code=user["postal_code"],
        budget=user["budget"],
        budget_period=user.get("budget_period") or "monthly",
        household_size=user["household_size"],
        dietary_restrictions=user["dietary_restrictions"],
        created_at=user["created_at"],
        is_active=user["is_active"]
    )


@router.get("/{user_id}/budget",
            response_model=BudgetStatusResponse,
            responses={
                404: {"model": ErrorResponse, "description": "User not found"},
                500: {"model": ErrorResponse, "description": "Internal server error"}
            })
async def get_budget_status(user_id: int) -> BudgetStatusResponse:
    """
    How much of the user's spend limit is left in the current period.

    `users.budget` is the ceiling for one period of length `budget_period`,
    not for one meal plan. `spent` is the summed `total_cost` of the shopping
    lists this user has **marked complete** inside the current period — lists
    that exist but were never bought are plans, not spend, and do not count.

    The period is calendar-anchored and half-open: `period_start` inclusive,
    `period_end` exclusive. Monthly runs from the 1st at 00:00; weekly from
    Monday at 00:00. Nothing is stored: `spent` is derived from the rows on
    every call, so there is no running total that can drift away from them.

    `remaining` goes negative when the user is over the limit — that is
    reported, not clamped. `available_for_plan` is the same figure floored at
    zero, and is what the generation path is allowed to spend.
    """
    user = UserService.get_user_by_id(user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )

    try:
        return BudgetStatusResponse(**budget_status(user))
    except Exception as e:
        logger.error(f"Error computing budget status for user {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to compute budget status"
        )


@router.put("/{user_id}",
            response_model=UserResponse,
            responses={
                404: {"model": ErrorResponse, "description": "User not found"},
                400: {"model": ErrorResponse, "description": "Invalid input"},
                422: {"model": ErrorResponse, "description": "Unsupported dietary restriction"}
            })
async def update_user(user_id: int, user_data: UserUpdate) -> UserResponse:
    """
    Update user preferences.

    Update user's postal code, budget, budget period, household size, or
    dietary restrictions. Only provided fields will be updated.

    Changing `budget_period` re-anchors the window immediately — the next read
    of `GET /users/{user_id}/budget` reports spend for the *new* period
    containing now, which may differ sharply from the old one. Past spend is
    not re-dated; only the window over it changes.

    `dietary_restrictions`, when given, accepts only labels from
    `GET /users/dietary-restrictions`; any other label is refused with 422.
    """
    if user_data.dietary_restrictions is not None:
        _refuse_unsupported(user_data.dietary_restrictions)
    try:
        user = UserService.update_user(user_id, user_data)

        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found"
            )

        return UserResponse(
            user_id=user["user_id"],
            email=user["email"],
            postal_code=user["postal_code"],
            budget=user["budget"],
            budget_period=user.get("budget_period") or "monthly",
            household_size=user["household_size"],
            dietary_restrictions=user["dietary_restrictions"],
            created_at=user["created_at"],
            is_active=user["is_active"]
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating user {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user"
        )


@router.delete("/{user_id}",
               status_code=status.HTTP_204_NO_CONTENT,
               responses={
                   404: {"model": ErrorResponse, "description": "User not found"}
               })
async def delete_user(user_id: int):
    """
    Deactivate user account.

    Soft deletes user account by setting is_active to false.
    User data is retained for historical purposes.
    """
    success = UserService.deactivate_user(user_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with ID {user_id} not found"
        )

    return None
