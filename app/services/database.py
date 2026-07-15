from decimal import Decimal
from typing import List, Dict, Optional
import json

# Import the connection pool from db module
from app.db import db


def _jsonable(row: Dict) -> Dict:
    """Convert DB row values json.dumps chokes on (Decimal) to plain floats."""
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}

class DatabaseService:
    """Handle all database interactions for recipe generation (uses shared connection pool)."""

    def __init__(self):
        # Use the shared database connection pool
        self.db = db

    def get_connection(self):
        """Create database connection context manager."""
        return self.db.get_connection()

    def fetch_current_deals(self, postal_code: str) -> List[Dict]:
        """
        Fetch all current deals for a postal code.

        Query deals table for valid offers, joining with stores.
        """
        query = """
        SELECT
            d.deal_id,
            d.product_name,
            d.sale_price,
            d.regular_price,
            d.discount_percentage,
            s.name as store_name,
            s.store_id,
            s.chain
        FROM deals d
        JOIN stores s ON d.store_id = s.store_id
        WHERE s.postal_code = %s
          AND d.valid_until >= CURRENT_DATE
          AND d.valid_from <= CURRENT_DATE
        ORDER BY d.discount_percentage DESC
        LIMIT 200;  -- Cap for performance
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (postal_code,))
            results = cursor.fetchall()
            return [_jsonable(dict(row)) for row in results]

    def save_recipes(self, user_id: int, recipes: List[Dict]) -> List[int]:
        """
        Save generated recipes to database.

        Returns list of recipe_ids.
        """
        query = """
        INSERT INTO recipes (
            user_id, name, ingredients, instructions,
            total_cost, servings, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
        RETURNING recipe_id;
        """

        recipe_ids = []
        with self.db.get_cursor() as cursor:
            for recipe in recipes:
                cursor.execute(query, (
                    user_id,
                    recipe['name'],
                    json.dumps(recipe['ingredients']),  # JSONB
                    recipe['instructions'],  # TEXT[]
                    recipe['total_cost'],
                    recipe['servings']
                ))
                recipe_id = cursor.fetchone()["recipe_id"]
                recipe_ids.append(recipe_id)

        return recipe_ids

    def get_recipe(self, recipe_id: int) -> Optional[Dict]:
        """
        Fetch a single recipe by id.
        """
        query = """
        SELECT recipe_id, user_id, name, ingredients, instructions,
               total_cost, servings, prep_time, cook_time, cuisine_type,
               meal_type, nutritional_info, allergen_info, created_at
        FROM recipes
        WHERE recipe_id = %s;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (recipe_id,))
            result = cursor.fetchone()
            return _jsonable(dict(result)) if result else None

    def get_user_recipes(self, user_id: int, limit: int = 10) -> List[Dict]:
        """
        Fetch a user's recipes, most-recent first.
        """
        query = """
        SELECT recipe_id, user_id, name, ingredients, instructions,
               total_cost, servings, prep_time, cook_time, cuisine_type,
               meal_type, nutritional_info, allergen_info, created_at
        FROM recipes
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT %s;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (user_id, limit))
            results = cursor.fetchall()
            return [_jsonable(dict(row)) for row in results]

    def save_shopping_list(
        self,
        user_id: int,
        recipe_ids: List[int],
        items: List[Dict],
        total_cost: float,
        estimated_savings: float,
        regular_total: float
    ) -> int:
        """
        Save a consolidated shopping list to the database.

        Returns the new list_id.
        """
        query = """
        INSERT INTO shopping_lists (
            user_id, recipe_ids, items, total_cost,
            estimated_savings, regular_total, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
        RETURNING list_id;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (
                user_id,
                recipe_ids,  # binds as Postgres INTEGER[]
                json.dumps(items),  # JSONB
                total_cost,
                estimated_savings,
                regular_total
            ))
            return cursor.fetchone()["list_id"]

    def get_latest_shopping_list(self, user_id: int) -> Optional[Dict]:
        """
        Fetch the most recent shopping list for a user.
        """
        query = """
        SELECT list_id, user_id, recipe_ids, items, total_cost,
               estimated_savings, regular_total, created_at, is_completed
        FROM shopping_lists
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT 1;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            return _jsonable(dict(result)) if result else None

    def mark_shopping_list_complete(self, user_id: int) -> bool:
        """
        Mark the user's most recent shopping list as completed.

        Returns whether a row was updated.
        """
        query = """
        UPDATE shopping_lists
        SET is_completed = true
        WHERE list_id = (
            SELECT list_id FROM shopping_lists
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        )
        RETURNING list_id;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            return result is not None
