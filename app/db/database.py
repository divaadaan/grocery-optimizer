"""
Database connection management
"""

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from contextlib import contextmanager
from typing import Generator, Optional
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class Database:
    """Database connection manager with connection pooling."""

    def __init__(self):
        self.pool: Optional[SimpleConnectionPool] = None

    def initialize(self):
        """Initialize database connection pool."""
        try:
            self.pool = SimpleConnectionPool(
                minconn=1,
                maxconn=settings.database_pool_size,
                dsn=settings.database_url
            )
            logger.info("Database connection pool initialized")
        except Exception as e:
            logger.error(f"Failed to initialize database pool: {e}")
            raise

    def close(self):
        """Close all database connections."""
        if self.pool:
            self.pool.closeall()
            logger.info("Database connection pool closed")

    @contextmanager
    def get_connection(self) -> Generator:
        """
        Context manager for database connections.

        Usage:
            with db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT ...")
        """
        if not self.pool:
            self.initialize()

        conn = self.pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {e}")
            raise
        finally:
            self.pool.putconn(conn)

    @contextmanager
    def get_cursor(self, cursor_factory=RealDictCursor) -> Generator:
        """
        Context manager for database cursor.

        Usage:
            with db.get_cursor() as cursor:
                cursor.execute("SELECT ...")
                results = cursor.fetchall()
        """
        with self.get_connection() as conn:
            cursor = conn.cursor(cursor_factory=cursor_factory)
            try:
                yield cursor
            finally:
                cursor.close()


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
