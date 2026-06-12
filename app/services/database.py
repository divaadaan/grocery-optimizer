from decimal import Decimal
from typing import List, Dict
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
