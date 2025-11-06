"""
Models module
"""

from app.models.schemas import (
    UserCreate,
    UserResponse,
    UserUpdate,
    StoreInfo,
    DealInfo,
    PostalCodeDiscoveryRequest,
    PostalCodeDiscoveryResponse,
    RecipeInfo,
    RecipeGenerationRequest,
    RecipeGenerationResponse,
    ShoppingListResponse,
    APIUsageStats,
    UserMetricsResponse,
    ErrorResponse,
    HealthCheckResponse
)

__all__ = [
    "UserCreate",
    "UserResponse",
    "UserUpdate",
    "StoreInfo",
    "DealInfo",
    "PostalCodeDiscoveryRequest",
    "PostalCodeDiscoveryResponse",
    "RecipeInfo",
    "RecipeGenerationRequest",
    "RecipeGenerationResponse",
    "ShoppingListResponse",
    "APIUsageStats",
    "UserMetricsResponse",
    "ErrorResponse",
    "HealthCheckResponse",
]
