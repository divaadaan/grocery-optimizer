"""
Database connection management
"""

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from contextlib import contextmanager
from typing import Generator, Optional
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class Database:
    """Database connection manager with connection pooling."""

    def __init__(self):
        self.pool: Optional[ConnectionPool] = None

    def initialize(self):
        """Initialize database connection pool."""
        try:
            self.pool = ConnectionPool(
                conninfo=settings.database_url,
                min_size=1,
                max_size=settings.database_pool_size,
                kwargs={"row_factory": dict_row},
                open=False,
            )
            self.pool.open(wait=True, timeout=30)
            logger.info("Database connection pool initialized")
        except Exception as e:
            logger.error(f"Failed to initialize database pool: {e}")
            raise

    def close(self):
        """Close all database connections."""
        if self.pool:
            self.pool.close()
            logger.info("Database connection pool closed")

    @contextmanager
    def get_connection(self) -> Generator:
        """
        Context manager for database connections.

        Commits on success, rolls back on exception (handled by the pool).

        Usage:
            with db.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT ...")
        """
        if not self.pool:
            self.initialize()

        try:
            with self.pool.connection() as conn:
                yield conn
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise

    @contextmanager
    def get_cursor(self, row_factory=None) -> Generator:
        """
        Context manager for database cursor. Rows are dicts by default.

        Usage:
            with db.get_cursor() as cursor:
                cursor.execute("SELECT ...")
                results = cursor.fetchall()
        """
        with self.get_connection() as conn:
            with conn.cursor(row_factory=row_factory) as cursor:
                yield cursor


# Global database instance
db = Database()


def get_db() -> Database:
    """Dependency for FastAPI routes."""
    return db


def init_db():
    """Initialize database connection (called on startup)."""
    db.initialize()


def close_db():
    """Close database connections (called on shutdown)."""
    db.close()
