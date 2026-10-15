from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Optional
import json

# Import the connection pool from db module
from app.db import db
from app.services.deal_gate import deal_filter


def _jsonable(row: Dict) -> Dict:
    """Convert DB row values json.dumps chokes on (Decimal) to plain floats."""
    return {k: float(v) if isinstance(v, Decimal) else v for k, v in row.items()}


class DatabaseService:
    """Handle all database interactions for recipe generation (shared pool)."""

    def __init__(self):
        # Use the shared database connection pool
        self.db = db

    def get_connection(self):
        """Create database connection context manager."""
        return self.db.get_connection()

    def fetch_current_deals(
        self,
        postal_code: str,
        dietary_restrictions: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        Fetch all current deals for a postal code.

        Query deals table for valid offers, joining with stores.

        ``dietary_restrictions`` is the DIETARY GATE. When the user has any
        restriction, only deals whose linked products are confirmed and clear
        the restrictions by their curated attributes are returned
        (``app/services/deal_gate.py``). This is the only dietary filter on
        the deal set; the Chef does not re-judge product names.

        Passing ``None`` means "no user context" and returns every deal -
        callers on the recipe-generation path MUST pass the user's
        restrictions.
        """

        query = """
        SELECT
            d.deal_id,
            d.product_name,
            d.sale_price,
            d.regular_price,
            d.discount_percentage,
            d.source,
            d.product_id,
            s.name as store_name,
            s.store_id,
            s.chain
        FROM deals d
        JOIN stores s ON d.store_id = s.store_id
        WHERE s.postal_code = %(postal_code)s
          AND d.valid_until >= CURRENT_DATE
          AND d.valid_from <= CURRENT_DATE
        """
        params: Dict = {"postal_code": postal_code}

        # Filtered in SQL rather than in Python so the LIMIT below is applied
        # to the *allowed* set. Filtering after the LIMIT could hand a
        # restricted user an empty deal list while permitted deals sat just
        # past row 200. Unlinked deals fail the gate's EXISTS and are withheld
        # — no link is never read as safe.
        fragment, extra = deal_filter(dietary_restrictions)
        query += fragment
        params.update(extra)

        # NULLS LAST matters now that real data exists: ingested deals usually
        # have no regular_price and therefore no discount_percentage, and
        # Postgres sorts NULLs FIRST on DESC - which would fill the whole
        # LIMIT with unmeasurable deals. Cheapest-first is the tie-break.
        query += """
        ORDER BY d.discount_percentage DESC NULLS LAST, d.sale_price ASC, d.deal_id
        LIMIT 200;  -- Cap for performance
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, params)
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
               estimated_savings, regular_total, created_at, is_completed,
               completed_at
        FROM shopping_lists
        WHERE user_id = %s
        ORDER BY created_at DESC
        LIMIT 1;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (user_id,))
            result = cursor.fetchone()
            return _jsonable(dict(result)) if result else None

    def mark_shopping_list_complete(
        self,
        user_id: int,
        list_id: Optional[int] = None,
    ) -> bool:
        """
        Mark one of the user's shopping lists as completed — the spend event.

        ``list_id`` names the list. Passing ``None`` keeps the original
        behaviour (the user's most recent list) for callers that predate the
        argument; it is the compatibility path, not the good one, because
        "latest" moves under a client that regenerates between shopping and
        reporting.

        ``user_id`` is always part of the WHERE clause, so one user can never
        complete another's list and distort their budget.

        ``completed_at`` is stamped here and is what budget accumulation reads
        (``app/services/budget.py``). COALESCE keeps the *first* completion
        time if a list is marked twice — a re-mark is not a second purchase.

        Returns whether a row was updated.
        """
        if list_id is not None:
            query = """
            UPDATE shopping_lists
            SET is_completed = true,
                completed_at = COALESCE(completed_at, NOW())
            WHERE list_id = %s AND user_id = %s
            RETURNING list_id;
            """
            params = (list_id, user_id)
        else:
            query = """
            UPDATE shopping_lists
            SET is_completed = true,
                completed_at = COALESCE(completed_at, NOW())
            WHERE list_id = (
                SELECT list_id FROM shopping_lists
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            )
            RETURNING list_id;
            """
            params = (user_id,)

        with self.db.get_cursor() as cursor:
            cursor.execute(query, params)
            result = cursor.fetchone()
            return result is not None

    def db_now(self) -> datetime:
        """The database's own wall clock, as a naive timestamp.

        Budget periods must be anchored in the same clock that writes
        ``completed_at``, and that is the database's: Neon runs UTC while the
        app process here runs local time, so a window computed from
        ``datetime.now()`` can be hours out of step with the timestamps it is
        filtering. Being consistently in the DB's clock means every completed
        list falls in exactly one period and the totals always reconcile.
        """
        with self.db.get_cursor() as cursor:
            cursor.execute("SELECT NOW()::timestamp AS now;")
            return cursor.fetchone()["now"]

    def get_completed_shopping_lists_since(
        self,
        user_id: int,
        since: datetime,
    ) -> List[Dict]:
        """
        Fetch the user's completed shopping lists with ``completed_at >= since``.

        Deliberately dumb: it narrows, it does not decide. The period
        arithmetic and the "does this row count as spend" rule live in
        ``app/services/budget.py`` so there is exactly one implementation of
        them, reachable by tests without a database. ``since`` is the period
        start, so this is a cheap pre-filter on the same bound the caller then
        applies — it can over-fetch (harmless), never under-fetch.

        No SUM here for the same reason: a total computed in SQL would be a
        second implementation of the rule.
        """
        query = """
        SELECT list_id, total_cost, completed_at, is_completed
        FROM shopping_lists
        WHERE user_id = %s
          AND is_completed = true
          AND completed_at IS NOT NULL
          AND completed_at >= %s
        ORDER BY completed_at;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (user_id, since))
            return [_jsonable(dict(row)) for row in cursor.fetchall()]
