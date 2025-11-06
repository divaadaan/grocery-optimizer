import psycopg2
from typing import List, Dict
import os
from dotenv import load_dotenv
import json

load_dotenv()

class DatabaseService:
    """Handle all database interactions for recipe generation."""

    def __init__(self):
        self.conn_string = os.getenv("DATABASE_URL")

    def get_connection(self):
        """Create database connection."""
        return psycopg2.connect(self.conn_string)

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

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (postal_code,))
                columns = [desc[0] for desc in cur.description]
                results = cur.fetchall()

                return [dict(zip(columns, row)) for row in results]

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
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                for recipe in recipes:
                    cur.execute(query, (
                        user_id,
                        recipe['name'],
                        json.dumps(recipe['ingredients']),  # JSONB
                        recipe['instructions'],  # TEXT[]
                        recipe['total_cost'],
                        recipe['servings']
                    ))
                    recipe_id = cur.fetchone()[0]
                    recipe_ids.append(recipe_id)

                conn.commit()

        return recipe_ids
