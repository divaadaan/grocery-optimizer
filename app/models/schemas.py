"""
Pydantic models for API request/response validation
"""

from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime, date
from decimal import Decimal


# ============================================================================
# User Schemas
# ============================================================================

BudgetPeriod = Literal["weekly", "monthly"]


class UserCreate(BaseModel):
    """Request model for user registration."""
    email: EmailStr
    postal_code: str = Field(..., min_length=6, max_length=10)
    budget: Decimal = Field(default=100.00, ge=0, le=10000)
    budget_period: BudgetPeriod = "monthly"
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
    budget_period: BudgetPeriod = "monthly"
    household_size: int
    dietary_restrictions: List[str]
    created_at: datetime
    is_active: bool


class DietaryRestrictionsResponse(BaseModel):
    """The restriction labels the app enforces from curated product
    attributes (``products.RESTRICTION_ATTRIBUTES``), sorted."""
    supported: List[str]


class UserUpdate(BaseModel):
    """Request model for updating user preferences."""
    postal_code: Optional[str] = None
    budget: Optional[Decimal] = Field(None, ge=0, le=10000)
    budget_period: Optional[BudgetPeriod] = None
    household_size: Optional[int] = Field(None, ge=1, le=20)
    dietary_restrictions: Optional[List[str]] = None


class BudgetStatusResponse(BaseModel):
    """How much of the user's spend limit is left in the current period.

    ``budget`` is the limit for ONE period of length ``budget_period``, and
    the period is calendar-anchored and half-open: ``period_start`` inclusive,
    ``period_end`` exclusive. ``spent`` is the summed ``total_cost`` of the
    shopping lists completed inside it — derived on read, never stored.

    ``remaining`` goes negative when the user is over; ``available_for_plan``
    is the same number floored at zero, and is what the generation path is
    allowed to spend.
    """
    user_id: int
    budget: Decimal
    budget_period: BudgetPeriod
    period_start: datetime
    period_end: datetime
    spent: Decimal
    remaining: Decimal
    available_for_plan: Decimal
    is_over_budget: bool
    completed_lists: int


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


class PlanBudgetCheck(BaseModel):
    """The finished plan's cost checked against the remaining period budget.

    ``plan_cost`` is a LOWER BOUND (each cited deal once; quantity, household
    size and multi-buy ignored), so the best this can say is
    ``within_lower_bound`` -- never that the plan is within budget.
    """
    status: Literal["within_lower_bound", "over"]
    plan_cost: Decimal
    budget: Decimal
    spent_this_period: Decimal
    available: Decimal
    headroom: Decimal
    budget_period: Optional[str] = None
    lower_bound: bool = True
    caveats: List[str] = Field(default_factory=list)


class RecipeGenerationResponse(BaseModel):
    """Response model for recipe generation."""
    recipes: List[RecipeInfo]
    # Shopping-list cost of the plan: each cited deal charged once however
    # many recipes reuse it (a recipe's own total_cost is "bought alone").
    # A lower bound -- see budget_check.caveats.
    total_cost: Decimal
    cost_per_meal: Decimal
    # null when no cited deal carries a regular price (unknown, not zero)
    estimated_savings: Optional[Decimal] = None
    generation_time: float
    status: str
    warnings: List[str] = Field(default_factory=list)
    # Additive (Track C). meals_requested vs len(recipes): partial is true
    # when fewer recipes were approved than asked for.
    meals_requested: Optional[int] = None
    partial: bool = False
    budget_check: Optional[PlanBudgetCheck] = None


class RecipeGenerationJobCreated(BaseModel):
    """Handle returned immediately by POST /recipes/generate."""
    job_id: str
    user_id: int
    state: str = Field(description="pending | running | completed | failed")
    created_at: datetime
    status_url: str = Field(description="Poll this URL for progress and the result")

    class Config:
        json_schema_extra = {
            "example": {
                "job_id": "9f1c2e5b7a4d4f0e8b3a6c1d2e5f7a90",
                "user_id": 1,
                "state": "pending",
                "created_at": "2026-10-14T14:00:00",
                "status_url": "/api/v1/recipes/generate/status/9f1c2e5b7a4d4f0e8b3a6c1d2e5f7a90"
            }
        }


class RecipeGenerationJobStatus(BaseModel):
    """Progress snapshot for a recipe-generation job."""
    job_id: str
    user_id: int
    state: str = Field(description="pending | running | completed | failed")
    stage: str = Field(description="Human-readable description of current work")
    stage_index: int = Field(description="1-based position in stage_total; 0 while queued")
    stage_total: int
    pipeline: List[str] = Field(
        default_factory=list,
        description="The nominal stage checklist, for rendering progress"
    )
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    elapsed_seconds: float
    result: Optional[RecipeGenerationResponse] = Field(
        default=None,
        description="Populated once state == completed; same shape the "
                    "synchronous endpoint used to return"
    )
    error: Optional[str] = Field(
        default=None,
        description="Populated once state == failed"
    )


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
    # null when no listed deal carries a regular price (unknown, not zero)
    estimated_savings: Optional[Decimal] = None
    stores: List[str]
    created_at: datetime
    is_completed: bool
    # When the user marked it bought — the spend date for budget
    # accumulation. None until then. Optional so existing clients that never
    # sent or read it keep working.
    completed_at: Optional[datetime] = None


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
