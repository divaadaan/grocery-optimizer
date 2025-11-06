"""
Pydantic models for API request/response validation
"""

from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime, date
from decimal import Decimal


# ============================================================================
# User Schemas
# ============================================================================

class UserCreate(BaseModel):
    """Request model for user registration."""
    email: EmailStr
    postal_code: str = Field(..., min_length=6, max_length=10)
    budget: Decimal = Field(default=100.00, ge=0, le=10000)
    household_size: int = Field(default=1, ge=1, le=20)
    dietary_restrictions: List[str] = Field(default_factory=list)

    @field_validator('postal_code')
    @classmethod
    def validate_postal_code(cls, v: str) -> str:
        """Remove spaces and convert to uppercase."""
        return v.replace(" ", "").upper()


class UserResponse(BaseModel):
    """Response model for user data."""
    user_id: int
    email: str
    postal_code: str
    budget: Decimal
    household_size: int
    dietary_restrictions: List[str]
    created_at: datetime
    is_active: bool


class UserUpdate(BaseModel):
    """Request model for updating user preferences."""
    postal_code: Optional[str] = None
    budget: Optional[Decimal] = Field(None, ge=0, le=10000)
    household_size: Optional[int] = Field(None, ge=1, le=20)
    dietary_restrictions: Optional[List[str]] = None


# ============================================================================
# Store & Deal Schemas
# ============================================================================

class StoreInfo(BaseModel):
    """Store information."""
    store_id: int
    name: str
    chain: Optional[str]
    postal_code: str
    address: Optional[str]
    city: Optional[str]
    province: Optional[str]


class DealInfo(BaseModel):
    """Deal information."""
    deal_id: int
    product_name: str
    brand: Optional[str]
    sale_price: Decimal
    regular_price: Decimal
    discount_percentage: int
    unit: Optional[str]
    category: Optional[str]
    valid_from: date
    valid_until: date
    store_name: str
    chain: Optional[str]


class PostalCodeDiscoveryRequest(BaseModel):
    """Request model for postal code discovery."""
    postal_code: str = Field(..., min_length=6, max_length=10)

    @field_validator('postal_code')
    @classmethod
    def validate_postal_code(cls, v: str) -> str:
        return v.replace(" ", "").upper()


class PostalCodeDiscoveryResponse(BaseModel):
    """Response model for postal code discovery."""
    postal_code: str
    stores_found: int
    deals_count: int
    stores: List[StoreInfo]
    job_id: Optional[str] = None
    message: str


# ============================================================================
# Recipe Schemas
# ============================================================================

class IngredientInfo(BaseModel):
    """Ingredient information within a recipe."""
    name: str
    quantity: str
    unit: str
    price: Decimal


class RecipeInfo(BaseModel):
    """Recipe information."""
    recipe_id: int
    name: str
    ingredients: List[Dict[str, Any]]
    instructions: List[str]
    total_cost: Decimal
    servings: int
    estimated_prep_time: Optional[int]
    meal_type: Optional[str]
    cuisine_type: Optional[str]
    nutrition_facts: Optional[Dict[str, Any]]
    health_score: Optional[Decimal]
    created_at: datetime


class RecipeGenerationRequest(BaseModel):
    """Request model for recipe generation."""
    user_id: int
    num_meals: int = Field(default=7, ge=1, le=21)
    preferences: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        json_schema_extra = {
            "example": {
                "user_id": 1,
                "num_meals": 7,
                "preferences": {
                    "cuisine_preferences": ["Italian", "Asian"],
                    "avoid_ingredients": ["mushrooms"],
                    "meal_types": ["dinner", "lunch"]
                }
            }
        }


class RecipeGenerationResponse(BaseModel):
    """Response model for recipe generation."""
    recipes: List[RecipeInfo]
    total_cost: Decimal
    cost_per_meal: Decimal
    estimated_savings: Decimal
    generation_time: float
    status: str
    warnings: List[str] = Field(default_factory=list)


# ============================================================================
# Shopping List Schemas
# ============================================================================

class ShoppingListItem(BaseModel):
    """Individual item in shopping list."""
    product: str
    quantity: str
    store: str
    price: Decimal
    category: Optional[str]


class ShoppingListResponse(BaseModel):
    """Response model for shopping list."""
    list_id: int
    user_id: int
    recipe_ids: List[int]
    items: List[Dict[str, Any]]
    total_cost: Decimal
    estimated_savings: Decimal
    stores: List[str]
    created_at: datetime
    is_completed: bool


# ============================================================================
# API Usage & Metrics Schemas
# ============================================================================

class APIUsageStats(BaseModel):
    """API usage statistics."""
    total_calls: int
    total_tokens: int
    total_cost: Decimal
    avg_cost_per_call: Decimal
    by_model: Dict[str, Dict[str, Any]]
    last_api_call: Optional[datetime]


class UserMetricsResponse(BaseModel):
    """Response model for user metrics."""
    user_id: int
    email: str
    api_usage: APIUsageStats
    recipe_stats: Dict[str, Any]
    total_savings: Decimal


# ============================================================================
# Error Response Schemas
# ============================================================================

class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class ValidationErrorDetail(BaseModel):
    """Validation error detail."""
    loc: List[str]
    msg: str
    type: str


class ValidationErrorResponse(BaseModel):
    """Validation error response."""
    error: str = "Validation Error"
    detail: List[ValidationErrorDetail]
    timestamp: datetime = Field(default_factory=datetime.now)


# ============================================================================
# Health Check Schema
# ============================================================================

class HealthCheckResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    database: str
    redis: Optional[str]
    ollama: Optional[str]
    timestamp: datetime = Field(default_factory=datetime.now)
