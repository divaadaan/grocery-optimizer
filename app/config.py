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

    # Flipp API
    flipp_api_key: Optional[str] = Field(default=None, env="FLIPP_API_KEY")
    flipp_api_url: str = Field(default="https://api.flipp.com/v2", env="FLIPP_API_URL")

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
