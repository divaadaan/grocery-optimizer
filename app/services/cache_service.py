"""
Redis caching service for performance optimization
"""

import redis
import json
import logging
from typing import Optional, Any
from functools import wraps

from app.config import settings

logger = logging.getLogger(__name__)


class CacheService:
    """Redis caching service with TTL support."""

    def __init__(self):
        self.redis_client: Optional[redis.Redis] = None
        self.enabled = settings.redis_enabled

        if self.enabled and settings.redis_url:
            try:
                self.redis_client = redis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )
                # Test connection
                self.redis_client.ping()
                logger.info("Redis cache initialized successfully")
            except Exception as e:
                logger.warning(f"Redis initialization failed: {e}")
                logger.warning("Continuing without cache")
                self.enabled = False
                self.redis_client = None
        else:
            logger.info("Redis caching disabled")

    def get(self, key: str) -> Optional[Any]:
        """
        Get value from cache.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found
        """
        if not self.enabled or not self.redis_client:
            return None

        try:
            value = self.redis_client.get(key)
            if value:
                logger.debug(f"Cache HIT: {key}")
                return json.loads(value)
            logger.debug(f"Cache MISS: {key}")
            return None
        except Exception as e:
            logger.error(f"Cache GET error for {key}: {e}")
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        """
        Set value in cache with optional TTL.

        Args:
            key: Cache key
            value: Value to cache (will be JSON serialized)
            ttl: Time to live in seconds (None = no expiration)

        Returns:
            True if successful, False otherwise
        """
        if not self.enabled or not self.redis_client:
            return False

        try:
            serialized = json.dumps(value)
            if ttl:
                self.redis_client.setex(key, ttl, serialized)
            else:
                self.redis_client.set(key, serialized)
            logger.debug(f"Cache SET: {key} (TTL: {ttl}s)")
            return True
        except Exception as e:
            logger.error(f"Cache SET error for {key}: {e}")
            return False

    def delete(self, key: str) -> bool:
        """Delete key from cache."""
        if not self.enabled or not self.redis_client:
            return False

        try:
            self.redis_client.delete(key)
            logger.debug(f"Cache DELETE: {key}")
            return True
        except Exception as e:
            logger.error(f"Cache DELETE error for {key}: {e}")
            return False

    def delete_pattern(self, pattern: str) -> int:
        """
        Delete all keys matching pattern.

        Args:
            pattern: Pattern to match (e.g., "user:*")

        Returns:
            Number of keys deleted
        """
        if not self.enabled or not self.redis_client:
            return 0

        try:
            keys = self.redis_client.keys(pattern)
            if keys:
                deleted = self.redis_client.delete(*keys)
                logger.debug(f"Cache DELETE pattern {pattern}: {deleted} keys")
                return deleted
            return 0
        except Exception as e:
            logger.error(f"Cache DELETE pattern error for {pattern}: {e}")
            return 0

    def clear(self) -> bool:
        """Clear all cache (use with caution!)."""
        if not self.enabled or not self.redis_client:
            return False

        try:
            self.redis_client.flushdb()
            logger.warning("Cache CLEARED")
            return True
        except Exception as e:
            logger.error(f"Cache CLEAR error: {e}")
            return False

    def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        if not self.enabled or not self.redis_client:
            return False

        try:
            return bool(self.redis_client.exists(key))
        except Exception as e:
            logger.error(f"Cache EXISTS error for {key}: {e}")
            return False

    def get_ttl(self, key: str) -> Optional[int]:
        """Get remaining TTL for key in seconds."""
        if not self.enabled or not self.redis_client:
            return None

        try:
            ttl = self.redis_client.ttl(key)
            return ttl if ttl >= 0 else None
        except Exception as e:
            logger.error(f"Cache TTL error for {key}: {e}")
            return None

    def increment(self, key: str, amount: int = 1) -> Optional[int]:
        """Increment numeric value in cache."""
        if not self.enabled or not self.redis_client:
            return None

        try:
            return self.redis_client.incrby(key, amount)
        except Exception as e:
            logger.error(f"Cache INCREMENT error for {key}: {e}")
            return None

    def close(self):
        """Close Redis connection."""
        if self.redis_client:
            try:
                self.redis_client.close()
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error(f"Error closing Redis connection: {e}")


# Global cache instance
cache = CacheService()


def cached(ttl: int, key_prefix: str = ""):
    """
    Decorator for caching function results.

    Args:
        ttl: Time to live in seconds
        key_prefix: Prefix for cache key

    Example:
        @cached(ttl=3600, key_prefix="deals")
        def get_deals(postal_code: str):
            return fetch_deals_from_db(postal_code)
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Build cache key from function name and arguments
            key_parts = [key_prefix or func.__name__]
            key_parts.extend([str(arg) for arg in args])
            key_parts.extend([f"{k}:{v}" for k, v in sorted(kwargs.items())])
            cache_key = ":".join(key_parts)

            # Try to get from cache
            cached_value = cache.get(cache_key)
            if cached_value is not None:
                return cached_value

            # Execute function and cache result
            result = func(*args, **kwargs)
            cache.set(cache_key, result, ttl)
            return result

        return wrapper
    return decorator


def invalidate_cache_pattern(pattern: str):
    """Helper to invalidate cache by pattern."""
    return cache.delete_pattern(pattern)


def get_cache() -> CacheService:
    """Dependency for FastAPI routes."""
    return cache


def init_cache():
    """Initialize cache (called on startup)."""
    # Cache is initialized in __init__, nothing to do here
    pass


def close_cache():
    """Close cache connection (called on shutdown)."""
    cache.close()
