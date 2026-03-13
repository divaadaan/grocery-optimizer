"""
Grocery Optimizer API
Main FastAPI application
"""

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from contextlib import asynccontextmanager
import logging
import time
from datetime import datetime

from app.config import settings
from app.db import init_db, close_db
from app.services.cache_service import init_cache, close_cache, cache
from app.models.schemas import HealthCheckResponse, ErrorResponse
from app.routes import users, stores, recipes, shopping_lists

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Lifespan context manager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifecycle.
    Initialize resources on startup, cleanup on shutdown.
    """
    # Startup
    logger.info("Starting Grocery Optimizer API")
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Database: {settings.database_url.split('@')[1] if '@' in settings.database_url else 'configured'}")

    try:
        init_db()
        logger.info("Database connection pool initialized")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

    # Initialize Redis cache
    try:
        init_cache()
        if cache.enabled:
            logger.info(f"Redis cache initialized (TTL: deals={settings.cache_ttl_deals}s, recipes={settings.cache_ttl_recipes}s)")
        else:
            logger.info("Redis cache disabled")
    except Exception as e:
        logger.warning(f"Redis initialization failed: {e}")
        logger.warning("Continuing without Redis cache")

    logger.info("Application startup complete")

    yield

    # Shutdown
    logger.info("Shutting down Grocery Optimizer API")
    close_db()
    logger.info("Database connections closed")
    close_cache()
    logger.info("Redis cache closed")
    logger.info("Shutdown complete")


# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="""
    Grocery Optimizer API - AI-powered meal planning with real-time grocery deals.

    ## Features

    * **User Management**: Register users with dietary preferences
    * **Deal Discovery**: Find grocery deals by postal code
    * **AI Recipe Generation**: Multi-agent system generates optimized meal plans
    * **Shopping Lists**: Consolidated, cost-optimized shopping lists

    ## Architecture

    - **Chef Orchestrator**: Plans ingredient groups for optimal reuse
    - **SousChef Agents**: Generate recipes in parallel
    - **Nutritionist Agent**: Validates recipes with feedback loop
    - **Shopping Optimizer**: Creates efficient shopping lists

    ## Tech Stack

    - FastAPI + PostgreSQL (TimescaleDB)
    - LangGraph for agent orchestration
    - Ollama with SmolLM models
    - MLflow for experiment tracking
    """,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url=f"{settings.api_prefix}/openapi.json"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: temporary - revert to settings.cors_origins
    allow_credentials=False,  # TODO: temporary - must be False when allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)


# Request timing middleware
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    """Add X-Process-Time header to all responses."""
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = f"{process_time:.4f}"
    return response


# Global exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle validation errors with detailed messages."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "Validation Error",
            "detail": exc.errors(),
            "body": exc.body,
            "timestamp": datetime.now().isoformat()
        }
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Handle unexpected errors gracefully."""
    logger.error(f"Unexpected error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal Server Error",
            "detail": "An unexpected error occurred. Please try again later.",
            "timestamp": datetime.now().isoformat()
        }
    )


# Include routers
app.include_router(users.router, prefix=settings.api_prefix)
app.include_router(stores.router, prefix=settings.api_prefix)
app.include_router(recipes.router, prefix=settings.api_prefix)
app.include_router(shopping_lists.router, prefix=settings.api_prefix)


# Root endpoint
@app.get("/", include_in_schema=False)
async def root():
    """Root endpoint with API information."""
    return {
        "name": settings.app_name,
        "version": settings.version,
        "environment": settings.environment,
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": f"{settings.api_prefix}/openapi.json",
        "status": "operational"
    }


# Health check endpoint
@app.get(
    "/health",
    response_model=HealthCheckResponse,
    tags=["health"],
    summary="Health check"
)
async def health_check() -> HealthCheckResponse:
    """
    Health check endpoint.

    Returns the status of the API and its dependencies.
    """
    from app.db import db

    # Check database
    db_status = "healthy"
    try:
        with db.get_cursor() as cursor:
            cursor.execute("SELECT 1;")
            cursor.fetchone()
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        db_status = "unhealthy"

    # Check Redis (if enabled)
    redis_status = None
    if settings.redis_enabled and cache.enabled:
        try:
            if cache.redis_client:
                cache.redis_client.ping()
                redis_status = "healthy"
            else:
                redis_status = "unavailable"
        except:
            redis_status = "unhealthy"
    elif settings.redis_enabled:
        redis_status = "disabled"

    # Check Ollama (optional)
    ollama_status = None
    try:
        import httpx
        response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=2.0)
        if response.status_code == 200:
            ollama_status = "healthy"
        else:
            ollama_status = "unhealthy"
    except:
        ollama_status = "unavailable"

    return HealthCheckResponse(
        status="healthy" if db_status == "healthy" else "degraded",
        version=settings.version,
        database=db_status,
        redis=redis_status,
        ollama=ollama_status,
        timestamp=datetime.now()
    )


# API Information endpoint
@app.get(
    f"{settings.api_prefix}/info",
    tags=["info"],
    summary="API Information"
)
async def api_info():
    """Get API configuration and feature flags."""
    return {
        "version": settings.version,
        "environment": settings.environment,
        "features": {
            "redis_caching": settings.redis_enabled,
            "cost_tracking": settings.enable_cost_tracking,
            "ollama_models": {
                "chef": settings.ollama_chef_model,
                "sous_chef": settings.ollama_sous_chef_model,
                "nutritionist": settings.ollama_nutritionist_model
            }
        },
        "limits": {
            "api_rate_limit": f"{settings.api_rate_limit} req/min",
            "cache_ttl_deals": f"{settings.cache_ttl_deals}s",
            "cache_ttl_recipes": f"{settings.cache_ttl_recipes}s"
        }
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level=settings.log_level.lower()
    )
