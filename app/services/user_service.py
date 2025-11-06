"""
User service for database operations
"""

from typing import Optional, Dict, Any
from datetime import datetime
import json
import logging

from app.db import db
from app.models.schemas import UserCreate, UserUpdate

logger = logging.getLogger(__name__)


class UserService:
    """Service for user-related database operations."""

    @staticmethod
    def create_user(user_data: UserCreate) -> Dict[str, Any]:
        """
        Create a new user in the database.

        Args:
            user_data: User creation data

        Returns:
            Dict containing user_id and user data

        Raises:
            Exception: If user with email already exists
        """
        query = """
        INSERT INTO users (
            email, postal_code, budget, household_size, dietary_restrictions
        ) VALUES (
            %(email)s, %(postal_code)s, %(budget)s, %(household_size)s, %(dietary_restrictions)s
        )
        RETURNING user_id, email, postal_code, budget, household_size,
                  dietary_restrictions, created_at, is_active;
        """

        try:
            with db.get_cursor() as cursor:
                cursor.execute(query, {
                    "email": user_data.email,
                    "postal_code": user_data.postal_code,
                    "budget": float(user_data.budget),
                    "household_size": user_data.household_size,
                    "dietary_restrictions": json.dumps(user_data.dietary_restrictions)
                })
                result = cursor.fetchone()

                if result:
                    logger.info(f"Created user: {result['email']}")
                    return dict(result)
                else:
                    raise Exception("Failed to create user")

        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise ValueError(f"User with email {user_data.email} already exists")
            logger.error(f"Error creating user: {e}")
            raise

    @staticmethod
    def get_user_by_id(user_id: int) -> Optional[Dict[str, Any]]:
        """Get user by ID."""
        query = """
        SELECT user_id, email, postal_code, budget, household_size,
               dietary_restrictions, created_at, last_login, is_active
        FROM users
        WHERE user_id = %(user_id)s AND is_active = true;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {"user_id": user_id})
            result = cursor.fetchone()
            return dict(result) if result else None

    @staticmethod
    def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
        """Get user by email."""
        query = """
        SELECT user_id, email, postal_code, budget, household_size,
               dietary_restrictions, created_at, last_login, is_active
        FROM users
        WHERE email = %(email)s AND is_active = true;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {"email": email})
            result = cursor.fetchone()
            return dict(result) if result else None

    @staticmethod
    def update_user(user_id: int, user_data: UserUpdate) -> Optional[Dict[str, Any]]:
        """Update user preferences."""
        # Build dynamic update query
        updates = []
        params = {"user_id": user_id}

        if user_data.postal_code:
            updates.append("postal_code = %(postal_code)s")
            params["postal_code"] = user_data.postal_code

        if user_data.budget is not None:
            updates.append("budget = %(budget)s")
            params["budget"] = float(user_data.budget)

        if user_data.household_size is not None:
            updates.append("household_size = %(household_size)s")
            params["household_size"] = user_data.household_size

        if user_data.dietary_restrictions is not None:
            updates.append("dietary_restrictions = %(dietary_restrictions)s")
            params["dietary_restrictions"] = json.dumps(user_data.dietary_restrictions)

        if not updates:
            return UserService.get_user_by_id(user_id)

        query = f"""
        UPDATE users
        SET {', '.join(updates)}, updated_at = NOW()
        WHERE user_id = %(user_id)s AND is_active = true
        RETURNING user_id, email, postal_code, budget, household_size,
                  dietary_restrictions, created_at, updated_at, is_active;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, params)
            result = cursor.fetchone()
            if result:
                logger.info(f"Updated user {user_id}")
                return dict(result)
            return None

    @staticmethod
    def update_last_login(user_id: int):
        """Update user's last login timestamp."""
        query = """
        UPDATE users
        SET last_login = NOW()
        WHERE user_id = %(user_id)s;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {"user_id": user_id})

    @staticmethod
    def deactivate_user(user_id: int) -> bool:
        """Soft delete user by setting is_active to false."""
        query = """
        UPDATE users
        SET is_active = false, updated_at = NOW()
        WHERE user_id = %(user_id)s
        RETURNING user_id;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {"user_id": user_id})
            result = cursor.fetchone()
            if result:
                logger.info(f"Deactivated user {user_id}")
                return True
            return False
