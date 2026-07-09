"""
Recipe generation endpoints
"""

from fastapi import APIRouter, HTTPException, status
import httpx
import logging
from datetime import datetime

from app.config import settings
from app.models.schemas import (
    RecipeGenerationRequest,
    RecipeGenerationResponse,
    RecipeInfo,
    ErrorResponse
)
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])


@router.post("/generate",
             response_model=RecipeGenerationResponse,
             responses={
                 404: {"model": ErrorResponse, "description": "User not found"},
                 500: {"model": ErrorResponse, "description": "Internal server error"}
             })
async def generate_recipes(request: RecipeGenerationRequest) -> RecipeGenerationResponse:
    """
    Generate optimized meal plan with recipes.

    Uses AI agents (Chef Orchestrator, SousChefs, Nutritionist) to generate
    a complete meal plan based on available deals and user preferences.

    - **user_id**: User ID for preferences and postal code
    - **num_meals**: Number of meals to generate (default: 7)
    - **preferences**: Optional preferences (cuisine types, meal types, ingredients to avoid)

    **Process:**
    1. Fetch user preferences and local deals
    2. Chef Orchestrator plans ingredient groups
    3. 3 SousChefs generate recipes in parallel
    4. Nutritionist validates recipes
    5. Return approved recipes with costs

    **Note:** This is currently a stub. Full LangGraph implementation will be added.
    """
    try:
        # Fail fast if Ollama is unreachable — otherwise the workflow spins for
        # minutes before surfacing an error the user can't act on.
        try:
            response = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=2.0)
            response.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"Ollama is not reachable at {settings.ollama_base_url}. "
                    "Start it (`ollama serve`) and try again."
                )
            )

        # Validate user exists
        user = UserService.get_user_by_id(request.user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {request.user_id} not found"
            )

        logger.info(f"Starting recipe generation for user {request.user_id}")

        # Run LangGraph recipe generation workflow
        import time
        start_time = time.time()

        from app.main_recipe_generation import run_recipe_generation

        try:
            result = run_recipe_generation(
                user_id=user["user_id"],
                postal_code=user["postal_code"],
                budget=float(user["budget"]),
                household_size=user["household_size"],
                dietary_restrictions=user["dietary_restrictions"],
                num_meals=request.num_meals,
                preferences=request.preferences
            )

            generation_time = time.time() - start_time

            if result["status"] != "completed":
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Recipe generation failed: {'; '.join(result.get('errors', ['Unknown error']))}"
                )

            # Convert recipes to response format
            recipes = []
            for recipe_id in result["approved_recipe_ids"]:
                recipe_data = result["generated_recipes"][recipe_id]
                recipes.append(RecipeInfo(
                    recipe_id=0,  # Will be updated after DB save
                    name=recipe_data["name"],
                    ingredients=recipe_data["ingredients"],
                    instructions=recipe_data["instructions"],
                    total_cost=recipe_data["total_cost"],
                    servings=recipe_data["servings"],
                    estimated_prep_time=recipe_data.get("estimated_prep_time"),
                    meal_type=recipe_data.get("meal_type"),
                    cuisine_type=recipe_data.get("cuisine_type"),
                    nutrition_facts=result["validation_results"].get(recipe_id, {}).get("nutrition_facts"),
                    health_score=result["validation_results"].get(recipe_id, {}).get("health_score"),
                    created_at=datetime.now()
                ))

            return RecipeGenerationResponse(
                recipes=recipes,
                total_cost=result["total_cost"],
                cost_per_meal=result["cost_per_meal"],
                estimated_savings=result["estimated_savings"],
                generation_time=generation_time,
                status=result["status"],
                warnings=result.get("warnings", [])
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in LangGraph workflow: {e}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Recipe generation workflow failed: {str(e)}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating recipes for user {request.user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate recipes"
        )


@router.get("/{recipe_id}",
            response_model=RecipeInfo,
            responses={
                404: {"model": ErrorResponse, "description": "Recipe not found"}
            })
async def get_recipe(recipe_id: int) -> RecipeInfo:
    """
    Get recipe by ID.

    Retrieve full recipe details including ingredients, instructions,
    cost breakdown, and nutritional information.
    """
    # TODO: Implement recipe retrieval from database
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Recipe retrieval not yet implemented"
    )


@router.get("/user/{user_id}",
            response_model=list[RecipeInfo],
            responses={
                404: {"model": ErrorResponse, "description": "User not found"}
            })
async def get_user_recipes(
    user_id: int,
    limit: int = 10
) -> list[RecipeInfo]:
    """
    Get all recipes for a user.

    Returns the most recent recipes generated for the user.

    - **user_id**: User ID
    - **limit**: Maximum number of recipes to return (default: 10)
    """
    # TODO: Implement user recipes retrieval
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="User recipes retrieval not yet implemented"
    )
