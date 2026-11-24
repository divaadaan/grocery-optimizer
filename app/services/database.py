from datetime import datetime
from decimal import Decimal
from typing import Any, List, Dict, Optional
import json
import uuid

# Import the connection pool from db module
from app.db import db
from app.services.deal_gate import deal_filter


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _jsonable(row: Dict) -> Dict:
    """Convert DB row values json.dumps chokes on to plain JSON types:
    Decimal -> float, UUID (generation_id) -> str."""
    return {k: _plain(v) for k, v in row.items()}


# Columns every recipe read returns (RecipeInfo is built from these).
# dietary_compliance is the verdict's only source (migration 023); the legacy
# allergen_info column it was copied from is deliberately not read.
_RECIPE_COLUMNS = """
    recipe_id, user_id, name, ingredients, instructions, instruction_steps,
    total_cost, servings, prep_time, cook_time, cuisine_type,
    meal_type, nutritional_info, dietary_compliance, health_score,
    generation_id, created_at
"""

_SHOPPING_LIST_COLUMNS = """
    list_id, user_id, recipe_ids, items, total_cost,
    estimated_savings, regular_total, created_at, is_completed,
    completed_at, generation_id
"""


_TRAY_PROVENANCE_SQL = """
UPDATE recipe_generations
SET offered_deal_ids = %(offered)s, kept_deal_ids = %(kept)s,
    pantry_product_ids = %(pantry)s, dish_plan = %(dish_plan)s
WHERE generation_id = %(generation_id)s
"""


class DatabaseService:
    """Handle all database interactions for recipe generation (shared pool)."""

    def __init__(self):
        # Use the shared database connection pool
        self.db = db

    def get_connection(self):
        """Create database connection context manager."""
        return self.db.get_connection()

    def fetch_gated_deals(
        self,
        postal_code: str,
        dietary_restrictions: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Every current deal for a postal code that clears the dietary gate.

        The tray builder's input (``app/services/trays.py``). There is no
        ranking and no cap: a ``discount_percentage`` ranking once kept every
        major chain (whose flyers carry no regular price) out of the Chef's
        view; the trays do the selecting now. Ordered by ``deal_id`` so the
        result is stable.

        ``dietary_restrictions`` is the DIETARY GATE (``deal_gate.deal_filter``),
        applied in SQL. When the user has any restriction, only deals whose
        linked products are confirmed and clear the restrictions by their
        curated attributes are returned; unlinked deals fail the gate's EXISTS
        and are withheld -- no link is never read as safe. ``None`` means "no
        user context" and returns every deal; the generation path always
        passes the user's restrictions.
        """
        query = """
        SELECT
            d.deal_id,
            d.product_name,
            d.sale_price,
            d.regular_price,
            d.unit,
            d.category,
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
        fragment, extra = deal_filter(dietary_restrictions)
        query += fragment
        params.update(extra)
        query += "\n        ORDER BY d.deal_id;"

        with self.db.get_cursor() as cursor:
            cursor.execute(query, params)
            return [_jsonable(dict(row)) for row in cursor.fetchall()]

    def save_recipes(self, user_id: int, recipes: List[Dict],
                     generation: Optional[Dict[str, Any]] = None) -> List[int]:
        """
        Save one generation's recipes, with its provenance, in one transaction.

        ``generation`` (``plan_pipeline``: ``_finalize`` / ``_fail``
        build it) becomes the ``recipe_generations`` row and its
        ``generation_id`` is stamped on every recipe (migration 022). A failed
        run is ``status='failed'`` with a ``failure_reason`` and no recipes
        (migration 023). ``None`` saves the recipes unlinked, as before 022
        -- no production path does that.

        Each recipe keeps everything the pipeline produced: ingredients with
        their ``deal_id`` / ``product_id`` citations exactly as generated
        (JSONB), the rendered ``instructions`` and the ``instruction_steps``
        behind them (JSONB), meal_type, cuisine_type, prep time,
        ``nutrition_facts`` / ``health_score`` (NULL since approach C dropped
        the Nutritionist), and its ``dietary_compliance`` verdict (its own column since 023;
        ``allergen_info`` is no longer written).

        Returns list of recipe_ids, in input order.
        """
        generation_id = (generation or {}).get("generation_id")

        generation_query = """
        INSERT INTO recipe_generations (
            generation_id, user_id, status, num_meals_requested,
            num_recipes_saved, dietary_restrictions, budget, budget_period,
            budget_spent_this_period, household_size, postal_code,
            preferences, models, snapshot, mlflow_run_id, total_cost,
            estimated_savings, failure_reason, iteration_count,
            num_recipes_generated, num_recipes_approved, num_recipes_rejected,
            created_at
        ) VALUES (
            %(generation_id)s, %(user_id)s, %(status)s, %(num_meals_requested)s,
            %(num_recipes_saved)s, %(dietary_restrictions)s, %(budget)s,
            %(budget_period)s, %(budget_spent_this_period)s, %(household_size)s,
            %(postal_code)s, %(preferences)s, %(models)s, %(snapshot)s,
            %(mlflow_run_id)s, %(total_cost)s, %(estimated_savings)s,
            %(failure_reason)s, %(iteration_count)s, %(num_recipes_generated)s,
            %(num_recipes_approved)s, %(num_recipes_rejected)s, NOW()
        );
        """

        query = """
        INSERT INTO recipes (
            user_id, name, ingredients, instructions, instruction_steps,
            total_cost, servings, prep_time, cuisine_type, meal_type,
            nutritional_info, dietary_compliance, health_score, generation_id,
            created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        RETURNING recipe_id;
        """

        def _json_or_none(value):
            return None if value is None else json.dumps(value)

        recipe_ids = []
        with self.db.get_cursor() as cursor:
            if generation_id is not None:
                cursor.execute(generation_query, {
                    "generation_id": generation_id,
                    "user_id": user_id,
                    "status": generation.get("status") or "completed",
                    "num_meals_requested": generation.get("num_meals_requested", len(recipes)),
                    "num_recipes_saved": len(recipes),
                    "dietary_restrictions": list(generation.get("dietary_restrictions") or []),
                    "budget": generation.get("budget"),
                    "budget_period": generation.get("budget_period"),
                    "budget_spent_this_period": generation.get("budget_spent_this_period"),
                    "household_size": generation.get("household_size"),
                    "postal_code": generation.get("postal_code"),
                    "preferences": json.dumps(generation.get("preferences") or {}),
                    "models": json.dumps(generation.get("models") or {}),
                    "snapshot": _json_or_none(generation.get("snapshot")),
                    "mlflow_run_id": generation.get("mlflow_run_id") or None,
                    "total_cost": generation.get("total_cost"),
                    "estimated_savings": generation.get("estimated_savings"),
                    "failure_reason": generation.get("failure_reason"),
                    "iteration_count": generation.get("iteration_count"),
                    "num_recipes_generated": generation.get("num_recipes_generated"),
                    "num_recipes_approved": generation.get("num_recipes_approved"),
                    "num_recipes_rejected": generation.get("num_recipes_rejected"),
                })
                if "kept_deal_ids" in generation:
                    # Approach C provenance (migration 024): what the trays
                    # offered, what the user kept, the confirmed pantry and the
                    # validated dish plan. Written only by the new pipeline, so
                    # the old path never needs 024.
                    cursor.execute(_TRAY_PROVENANCE_SQL, {
                        "generation_id": generation_id,
                        "offered": list(generation.get("offered_deal_ids") or []),
                        "kept": list(generation.get("kept_deal_ids") or []),
                        "pantry": list(generation.get("pantry_product_ids") or []),
                        "dish_plan": _json_or_none(generation.get("dish_plan")),
                    })
            for recipe in recipes:
                cursor.execute(query, (
                    user_id,
                    recipe['name'],
                    json.dumps(recipe['ingredients']),  # JSONB, citations intact
                    recipe['instructions'],  # TEXT[]
                    _json_or_none(recipe.get('instruction_steps')),  # JSONB
                    recipe['total_cost'],
                    recipe['servings'],
                    recipe.get('estimated_prep_time'),
                    recipe.get('cuisine_type'),
                    recipe.get('meal_type'),
                    _json_or_none(recipe.get('nutrition_facts')),
                    _json_or_none(recipe.get('dietary_compliance')),
                    recipe.get('health_score'),
                    generation_id,
                ))
                recipe_id = cursor.fetchone()["recipe_id"]
                recipe_ids.append(recipe_id)

        return recipe_ids

    def get_recipe(self, recipe_id: int) -> Optional[Dict]:
        """
        Fetch a single recipe by id.
        """
        query = f"""
        SELECT {_RECIPE_COLUMNS}
        FROM recipes
        WHERE recipe_id = %s;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (recipe_id,))
            result = cursor.fetchone()
            return _jsonable(dict(result)) if result else None

    def get_user_recipes(self, user_id: int, limit: int = 10,
                         generation_id: Optional[str] = None) -> List[Dict]:
        """
        Fetch a user's recipes, most-recent first.

        ``generation_id`` narrows to that one generation (still scoped to the
        user, so another user's generation returns nothing).
        """
        params: List[Any] = [user_id]
        where = "WHERE user_id = %s"
        if generation_id is not None:
            where += " AND generation_id = %s"
            params.append(generation_id)
        params.append(limit)
        query = f"""
        SELECT {_RECIPE_COLUMNS}
        FROM recipes
        {where}
        ORDER BY created_at DESC, recipe_id DESC
        LIMIT %s;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, tuple(params))
            results = cursor.fetchall()
            return [_jsonable(dict(row)) for row in results]

    def get_latest_generation_id(self, user_id: int) -> Optional[str]:
        """The user's most recent persisted meal plan, or None if they have
        none (no plan yet, or only recipes saved before migration 022).

        Failed runs (migration 023) have a row but no recipes, so they are
        skipped: a failed regeneration must not hide the last real plan."""
        query = """
        SELECT generation_id
        FROM recipe_generations
        WHERE user_id = %s AND status <> 'failed'
        ORDER BY created_at DESC
        LIMIT 1;
        """
        with self.db.get_cursor() as cursor:
            cursor.execute(query, (user_id,))
            row = cursor.fetchone()
            return _plain(row["generation_id"]) if row else None

    def get_generation_recipes(self, user_id: int, generation_id: str) -> List[Dict]:
        """Every recipe of one of the user's generations, in save order."""
        query = f"""
        SELECT {_RECIPE_COLUMNS}
        FROM recipes
        WHERE user_id = %s AND generation_id = %s
        ORDER BY recipe_id;
        """
        with self.db.get_cursor() as cursor:
            cursor.execute(query, (user_id, generation_id))
            return [_jsonable(dict(row)) for row in cursor.fetchall()]

    def save_shopping_list(
        self,
        user_id: int,
        recipe_ids: List[int],
        items: List[Dict],
        total_cost: float,
        estimated_savings: Optional[float],
        regular_total: Optional[float],
        generation_id: Optional[str] = None,
    ) -> int:
        """
        Save a consolidated shopping list to the database.

        ``generation_id`` is the generation the list was built from.

        Returns the new list_id.
        """
        query = """
        INSERT INTO shopping_lists (
            user_id, recipe_ids, items, total_cost,
            estimated_savings, regular_total, generation_id, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
        RETURNING list_id;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, (
                user_id,
                recipe_ids,  # binds as Postgres INTEGER[]
                json.dumps(items),  # JSONB
                total_cost,
                estimated_savings,
                regular_total,
                generation_id,
            ))
            return cursor.fetchone()["list_id"]

    def get_latest_shopping_list(self, user_id: int,
                                 generation_id: Optional[str] = None) -> Optional[Dict]:
        """
        Fetch the most recent shopping list for a user -- of one generation
        when ``generation_id`` is given.
        """
        params: List[Any] = [user_id]
        where = "WHERE user_id = %s"
        if generation_id is not None:
            where += " AND generation_id = %s"
            params.append(generation_id)
        query = f"""
        SELECT {_SHOPPING_LIST_COLUMNS}
        FROM shopping_lists
        {where}
        ORDER BY created_at DESC, list_id DESC
        LIMIT 1;
        """

        with self.db.get_cursor() as cursor:
            cursor.execute(query, tuple(params))
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
