"""
User endpoints
"""

from fastapi import APIRouter, HTTPException, status
import logging

from app.models.schemas import (
    UserCreate,
    UserResponse,
    UserUpdate,
    ErrorResponse
)
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/register",
             response_model=UserResponse,
             status_code=status.HTTP_201_CREATED,
             responses={
                 400: {"model": ErrorResponse, "description": "Invalid input or user already exists"},
                 500: {"model": ErrorResponse, "description": "Internal server error"}
             })
async def register_user(user_data: UserCreate) -> UserResponse:
    """
    Register a new user.

    Creates a new user account with dietary preferences and budget settings.

    - **email**: User's email address (must be unique)
    - **postal_code**: User's postal code for finding local deals
    - **budget**: Monthly grocery budget (default: $100.00)
    - **household_size**: Number of people in household (default: 1)
    - **dietary_restrictions**: List of dietary restrictions (e.g., ["vegetarian", "gluten_free"])
    """
    try:
        user = UserService.create_user(user_data)

        return UserResponse(
            user_id=user["user_id"],
            email=user["email"],
            postal_code=user["postal_code"],
            budget=user["budget"],
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
        household_size=user["household_size"],
        dietary_restrictions=user["dietary_restrictions"],
        created_at=user["created_at"],
        is_active=user["is_active"]
    )


@router.put("/{user_id}",
            response_model=UserResponse,
            responses={
                404: {"model": ErrorResponse, "description": "User not found"},
                400: {"model": ErrorResponse, "description": "Invalid input"}
            })
async def update_user(user_id: int, user_data: UserUpdate) -> UserResponse:
    """
    Update user preferences.

    Update user's postal code, budget, household size, or dietary restrictions.
    Only provided fields will be updated.
    """
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
            household_size=user["household_size"],
            dietary_restrictions=user["dietary_restrictions"],
            created_at=user["created_at"],
            is_active=user["is_active"]
        )

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
