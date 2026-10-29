"""
Application Configuration
Centralized settings loaded from environment variables
"""

from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    app_name: str = "Grocery Optimizer API"
    version: str = "0.1.0"
    environment: str = Field(default="development", env="ENVIRONMENT")
    debug: bool = Field(default=True, env="DEBUG")

    # API
    api_prefix: str = "/api/v1"
    secret_key: str = Field(default="dev-secret-key-change-in-production", env="SECRET_KEY")

    # Database
    database_url: str = Field(..., env="DATABASE_URL")
    database_pool_size: int = Field(default=10, env="DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(default=20, env="DATABASE_MAX_OVERFLOW")

    # Redis (optional)
    redis_url: Optional[str] = Field(default=None, env="REDIS_URL")
    redis_enabled: bool = Field(default=False, env="REDIS_ENABLED")

    # Cache TTL (seconds)
    cache_ttl_deals: int = Field(default=21600, env="CACHE_TTL_DEALS")  # 6 hours
    cache_ttl_recipes: int = Field(default=86400, env="CACHE_TTL_RECIPES")  # 24 hours
    cache_ttl_stores: int = Field(default=604800, env="CACHE_TTL_STORES")  # 7 days

    # LLM provider. Chat models are built only by app/agents/llm_factory.py,
    # which dispatches on this. "ollama" is the only provider implemented.
    llm_provider: str = Field(default="ollama", env="LLM_PROVIDER")

    # Ollama
    ollama_base_url: str = Field(default="http://localhost:11434", env="OLLAMA_BASE_URL")
    ollama_chef_model: str = Field(default="smollm:1.7b", env="OLLAMA_CHEF_MODEL")
    ollama_sous_chef_model: str = Field(default="smollm:360m", env="OLLAMA_SOUS_CHEF_MODEL")
    ollama_nutritionist_model: str = Field(default="smollm:360m", env="OLLAMA_NUTRITIONIST_MODEL")

    # MLflow
    mlflow_tracking_uri: str = Field(default="http://localhost:5000", env="MLFLOW_TRACKING_URI")
    mlflow_experiment_name: str = Field(default="grocery-meal-planner", env="MLFLOW_EXPERIMENT_NAME")

    # Hugging Face
    huggingface_api_key: Optional[str] = Field(default=None, env="HUGGINGFACE_API_KEY")

    # ------------------------------------------------------------------
    # Deal ingestion (Flipp flyer search)
    # ------------------------------------------------------------------
    # RETIRED: flipp_api_key / flipp_api_url. There is no Flipp API behind
    # them. FLIPP_API_KEY was never anything but the .env.example placeholder
    # ("your_flipp_api_key_here"), and https://api.flipp.com/v2 does not
    # correspond to any documented Flipp product. Nothing in the codebase ever
    # read either value. They are gone rather than left as decoration, so the
    # next reader does not spend an afternoon hunting for credentials that
    # cannot exist. Leaving the old keys in .env is harmless — Settings is
    # configured with extra="ignore".
    #
    # What replaces them is the keyless backend behind flipp.com's own search
    # box. It is UNSANCTIONED: undocumented, unlicensed, no SLA, and subject to
    # being blocked at any time. Hence off by default.
    # See app/services/flipp_client.py.
    deal_ingestion_enabled: bool = Field(default=False, env="DEAL_INGESTION_ENABLED")

    # There is deliberately no dietary-gate flag. Users with dietary
    # restrictions are always served through the products gate
    # (app/services/deal_gate.py); a switch could only turn safety off.

    flipp_search_url: str = Field(
        default="https://backflipp.wishabi.com/flipp/items/search",
        env="FLIPP_SEARCH_URL",
    )
    # Politeness settings for an endpoint that owes us nothing.
    flipp_timeout_seconds: float = Field(default=10.0, env="FLIPP_TIMEOUT_SECONDS")
    flipp_max_retries: int = Field(default=2, env="FLIPP_MAX_RETRIES")
    flipp_min_request_interval: float = Field(
        default=1.5, env="FLIPP_MIN_REQUEST_INTERVAL"
    )
    flipp_user_agent: str = Field(
        default="grocery-optimizer/0.1 (personal meal-planning project)",
        env="FLIPP_USER_AGENT",
    )

    # Cost Tracking
    enable_cost_tracking: bool = Field(default=True, env="ENABLE_COST_TRACKING")
    cost_smollm_1_7b: float = Field(default=0.001, env="COST_SMOLLM_1_7B")
    cost_smollm_360m: float = Field(default=0.0005, env="COST_SMOLLM_360M")
    cost_smollm_135m: float = Field(default=0.0002, env="COST_SMOLLM_135M")

    # Rate Limiting
    api_rate_limit: int = Field(default=100, env="API_RATE_LIMIT")  # requests per minute

    # CORS
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:8000"],
        env="CORS_ORIGINS"
    )

    # Logging
    log_level: str = Field(default="INFO", env="LOG_LEVEL")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"  # .env also holds vars for docker-compose/other tools


# Global settings instance
settings = Settings()
