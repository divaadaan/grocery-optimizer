"""
Store and deals service for database operations
"""

from typing import List, Dict, Any, Optional
from datetime import date
import logging

from app.db import db
from app.services.cache_service import cache
from app.config import settings

logger = logging.getLogger(__name__)


class StoreService:
    """Service for store and deal-related database operations."""

    @staticmethod
    def get_stores_by_postal_code(postal_code: str) -> List[Dict[str, Any]]:
        """Get all stores for a given postal code."""
        query = """
        SELECT store_id, name, chain, postal_code, address, city, province,
               latitude, longitude, last_updated
        FROM stores
        WHERE postal_code = %(postal_code)s AND is_active = true
        ORDER BY name;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {"postal_code": postal_code})
            results = cursor.fetchall()
            return [dict(row) for row in results]

    @staticmethod
    def get_current_deals_by_postal_code(postal_code: str, category: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all current deals for stores in a postal code.
        Uses Redis caching for performance.

        Args:
            postal_code: Postal code to search
            category: Optional category filter

        Returns:
            List of deals with store information
        """
        # Try cache first
        cache_key = f"deals:{postal_code}:{category or 'all'}"
        cached_deals = cache.get(cache_key)
        if cached_deals is not None:
            logger.debug(f"Returning {len(cached_deals)} deals from cache")
            return cached_deals

        query = """
        SELECT
            d.deal_id,
            d.product_name,
            d.brand,
            d.sale_price,
            d.regular_price,
            d.discount_percentage,
            d.unit,
            d.category,
            d.valid_from,
            d.valid_until,
            s.store_id,
            s.name as store_name,
            s.chain,
            s.postal_code,
            s.address
        FROM deals d
        JOIN stores s ON d.store_id = s.store_id
        WHERE s.postal_code = %(postal_code)s
          AND d.valid_until >= CURRENT_DATE
          AND d.valid_from <= CURRENT_DATE
          AND s.is_active = true
        """

        params = {"postal_code": postal_code}

        if category:
            query += " AND d.category = %(category)s"
            params["category"] = category

        query += " ORDER BY d.discount_percentage DESC, d.sale_price ASC;"

        with db.get_cursor() as cursor:
            cursor.execute(query, params)
            results = cursor.fetchall()
            deals = [dict(row) for row in results]
            logger.info(f"Found {len(deals)} deals for postal code {postal_code}")

            # Cache the results
            cache.set(cache_key, deals, ttl=settings.cache_ttl_deals)

            return deals

    @staticmethod
    def get_deals_by_category(postal_code: str, categories: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Get deals grouped by category."""
        query = """
        SELECT
            d.deal_id,
            d.product_name,
            d.brand,
            d.sale_price,
            d.regular_price,
            d.discount_percentage,
            d.unit,
            d.category,
            d.valid_until,
            s.name as store_name,
            s.chain
        FROM deals d
        JOIN stores s ON d.store_id = s.store_id
        WHERE s.postal_code = %(postal_code)s
          AND d.category = ANY(%(categories)s)
          AND d.valid_until >= CURRENT_DATE
          AND s.is_active = true
        ORDER BY d.category, d.discount_percentage DESC;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {
                "postal_code": postal_code,
                "categories": categories
            })
            results = cursor.fetchall()

            # Group by category
            grouped = {}
            for row in results:
                category = row['category']
                if category not in grouped:
                    grouped[category] = []
                grouped[category].append(dict(row))

            return grouped

    @staticmethod
    def get_top_deals(postal_code: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Get top deals by discount percentage."""
        query = """
        SELECT
            d.product_name,
            d.brand,
            d.sale_price,
            d.regular_price,
            d.discount_percentage,
            d.unit,
            d.category,
            s.name as store_name,
            s.chain
        FROM deals d
        JOIN stores s ON d.store_id = s.store_id
        WHERE s.postal_code = %(postal_code)s
          AND d.valid_until >= CURRENT_DATE
          AND s.is_active = true
        ORDER BY d.discount_percentage DESC, d.sale_price ASC
        LIMIT %(limit)s;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {
                "postal_code": postal_code,
                "limit": limit
            })
            results = cursor.fetchall()
            return [dict(row) for row in results]

    @staticmethod
    def search_deals(postal_code: str, search_term: str) -> List[Dict[str, Any]]:
        """Search deals by product name or brand."""
        query = """
        SELECT
            d.deal_id,
            d.product_name,
            d.brand,
            d.sale_price,
            d.regular_price,
            d.discount_percentage,
            d.unit,
            d.category,
            s.name as store_name,
            s.chain
        FROM deals d
        JOIN stores s ON d.store_id = s.store_id
        WHERE s.postal_code = %(postal_code)s
          AND (
              d.product_name ILIKE %(search_pattern)s
              OR d.brand ILIKE %(search_pattern)s
          )
          AND d.valid_until >= CURRENT_DATE
          AND s.is_active = true
        ORDER BY d.discount_percentage DESC
        LIMIT 50;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {
                "postal_code": postal_code,
                "search_pattern": f"%{search_term}%"
            })
            results = cursor.fetchall()
            return [dict(row) for row in results]

    @staticmethod
    def get_deal_statistics(postal_code: str) -> Dict[str, Any]:
        """Get statistics about deals in a postal code."""
        query = """
        SELECT
            COUNT(*) as total_deals,
            COUNT(DISTINCT d.category) as total_categories,
            COUNT(DISTINCT s.store_id) as total_stores,
            AVG(d.discount_percentage) as avg_discount,
            MAX(d.discount_percentage) as max_discount,
            MIN(d.sale_price) as min_price,
            MAX(d.sale_price) as max_price
        FROM deals d
        JOIN stores s ON d.store_id = s.store_id
        WHERE s.postal_code = %(postal_code)s
          AND d.valid_until >= CURRENT_DATE
          AND s.is_active = true;
        """

        with db.get_cursor() as cursor:
            cursor.execute(query, {"postal_code": postal_code})
            result = cursor.fetchone()
            return dict(result) if result else {}
