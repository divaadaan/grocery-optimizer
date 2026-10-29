"""
Recipe generation endpoints
"""

from fastapi import APIRouter, HTTPException, status
import logging
from datetime import datetime

from app.config import settings
from app.models.schemas import (
    RecipeGenerationRequest,
    RecipeGenerationResponse,
    RecipeGenerationJobCreated,
    RecipeGenerationJobStatus,
    RecipeInfo,
    ErrorResponse
)
from app.services import products
from app.services.budget import DEFAULT_PERIOD, budget_status
from app.services.user_service import UserService
from app.services.database import DatabaseService
from app.services.recipe_jobs import (
    COMPLETED,
    PIPELINE_STAGES,
    RecipeJob,
    job_registry,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recipes", tags=["recipes"])

db_service = DatabaseService()


def _recipe_row_to_info(recipe: dict) -> RecipeInfo:
    """Map a `recipes` table row to the RecipeInfo response model."""
    return RecipeInfo(
        recipe_id=recipe["recipe_id"],
        name=recipe["name"],
        ingredients=recipe["ingredients"],
        instructions=recipe["instructions"],
        total_cost=recipe["total_cost"],
        servings=recipe["servings"],
        estimated_prep_time=recipe.get("prep_time"),
        meal_type=recipe.get("meal_type"),
        cuisine_type=recipe.get("cuisine_type"),
        nutrition_facts=recipe.get("nutritional_info"),
        health_score=None,  # Not stored on the recipes table
        created_at=recipe["created_at"]
    )


def _build_generation_response(
    result: dict,
    generation_time: float
) -> RecipeGenerationResponse:
    """
    Map a finished graph state to the recipe-generation response payload.

    This is the contract the synchronous endpoint used to return directly;
    it now travels to the client inside the job's `result` field, unchanged.
    """
    # saved_recipe_ids (set by finalize_meal_plan) aligns in order with
    # approved_recipe_ids; fall back to 0 defensively if it's missing (e.g.
    # partial/failure paths that never reached finalize).
    saved_recipe_ids = result.get("saved_recipe_ids", [])
    recipe_id_map = dict(zip(result["approved_recipe_ids"], saved_recipe_ids))

    recipes = []
    for recipe_id in result["approved_recipe_ids"]:
        recipe_data = result["generated_recipes"][recipe_id]
        recipes.append(RecipeInfo(
            recipe_id=recipe_id_map.get(recipe_id, 0),
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


def _run_generation_job(job_id: str, user: dict, request: RecipeGenerationRequest) -> None:
    """
    Worker body for one generation job. Runs on the registry's thread pool,
    never on the event loop, so it may block freely.
    """
    import time

    # Imported here (not at module scope) so the agent/LangGraph stack is
    # only pulled in when a generation actually runs, and so tests can patch
    # the module attribute.
    from app import main_recipe_generation

    job_registry.mark_running(job_id)
    start_time = time.time()

    # Read the spend already committed in this budget period, at run time
    # rather than at request time, so a list completed while the job queued is
    # counted. `budget` is the period ceiling, not a per-generation allowance:
    # without this the plan is costed against the whole week's or month's money
    # however much of it is already gone.
    try:
        status_now = budget_status(user)
        budget_period = status_now["budget_period"]
        budget_spent = status_now["spent"]
    except Exception as e:
        # A budget read failure must not sink the generation. Falling back to
        # 0.0 restores the old (over-generous) ceiling, and says so loudly
        # instead of silently.
        logger.error(
            f"Could not read budget status for user {user['user_id']}: {e}. "
            "Costing this plan against the full period budget.", exc_info=True
        )
        budget_period = DEFAULT_PERIOD
        budget_spent = 0.0

    try:
        result = main_recipe_generation.run_recipe_generation(
            user_id=user["user_id"],
            postal_code=user["postal_code"],
            budget=float(user["budget"]),
            budget_period=budget_period,
            budget_spent_this_period=budget_spent,
            household_size=user["household_size"],
            dietary_restrictions=user["dietary_restrictions"],
            num_meals=request.num_meals,
            preferences=request.preferences,
            progress_callback=lambda node: job_registry.report_node(job_id, node),
        )
    except Exception as e:
        logger.error(f"Error in LangGraph workflow for job {job_id}: {e}", exc_info=True)
        job_registry.fail(job_id, f"Recipe generation workflow failed: {e}")
        return

    if result.get("status") != COMPLETED:
        errors = result.get("errors") or ["Unknown error"]
        job_registry.fail(job_id, f"Recipe generation failed: {'; '.join(errors)}")
        return

    try:
        response = _build_generation_response(result, time.time() - start_time)
    except Exception as e:
        logger.error(f"Error building response for job {job_id}: {e}", exc_info=True)
        job_registry.fail(job_id, f"Recipe generation produced an unusable result: {e}")
        return

    job_registry.complete(job_id, response.model_dump())
    logger.info(f"Recipe job {job_id} completed with {len(response.recipes)} recipes")


def _status_url(job_id: str) -> str:
    return f"{settings.api_prefix}/recipes/generate/status/{job_id}"


def _job_status(job: RecipeJob) -> RecipeGenerationJobStatus:
    return RecipeGenerationJobStatus(
        job_id=job.job_id,
        user_id=job.user_id,
        state=job.state,
        stage=job.stage,
        stage_index=job.stage_index,
        stage_total=len(PIPELINE_STAGES),
        pipeline=list(PIPELINE_STAGES),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        elapsed_seconds=job.elapsed_seconds,
        result=job.result,
        error=job.error,
    )


@router.post("/generate",
             response_model=RecipeGenerationJobCreated,
             status_code=status.HTTP_202_ACCEPTED,
             responses={
                 404: {"model": ErrorResponse, "description": "User not found"},
                 422: {"model": ErrorResponse, "description": "Profile carries an unsupported dietary restriction"},
                 500: {"model": ErrorResponse, "description": "Internal server error"}
             })
async def generate_recipes(request: RecipeGenerationRequest) -> RecipeGenerationJobCreated:
    """
    Start generating an optimized meal plan. Returns immediately with a job.

    Generation uses AI agents (Chef Orchestrator, SousChefs, Nutritionist) and
    takes minutes with local models, so this endpoint does **not** wait for it:
    it validates the request, queues the work, and returns a `job_id`. Poll
    `GET /recipes/generate/status/{job_id}` for progress and the result.

    - **user_id**: User ID for preferences and postal code
    - **num_meals**: Number of meals to generate (default: 7)
    - **preferences**: Optional preferences (cuisine types, meal types, ingredients to avoid)

    **Process (reported as progress stages):**
    1. Fetch user preferences and local deals
    2. Chef Orchestrator plans ingredient groups
    3. 3 SousChefs generate recipes in parallel
    4. Nutritionist validates recipes (rejected ones are retried)
    5. Approved recipes are persisted and returned in the job result
    """
    try:
        # Validate user exists before accepting the job — a bad user_id is a
        # 404 here rather than a job that fails a minute later.
        user = UserService.get_user_by_id(request.user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {request.user_id} not found"
            )

        # A stored profile can predate the user API refusing unsupported
        # labels. Refuse here, before a job exists, rather than generate
        # recipes "approved" for a restriction nothing checks.
        unsupported = products.unsupported_restrictions(user.get("dietary_restrictions"))
        if unsupported:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(products.UnsupportedRestrictionError(unsupported))
                + " Update the profile to remove them, then generate again.",
            )

        job = job_registry.create(user_id=user["user_id"], num_meals=request.num_meals)
        job_registry.submit(job.job_id, lambda: _run_generation_job(job.job_id, user, request))

        logger.info(f"Queued recipe generation job {job.job_id} for user {request.user_id}")

        return RecipeGenerationJobCreated(
            job_id=job.job_id,
            user_id=job.user_id,
            state=job.state,
            created_at=job.created_at,
            status_url=_status_url(job.job_id),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting recipe generation for user {request.user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to start recipe generation"
        )


@router.get("/generate/status/{job_id}",
            response_model=RecipeGenerationJobStatus,
            responses={
                404: {"model": ErrorResponse, "description": "Job not found"}
            })
async def get_generation_status(job_id: str) -> RecipeGenerationJobStatus:
    """
    Poll a recipe-generation job.

    Returns the job `state` (`pending`, `running`, `completed`, `failed`), the
    current `stage` (a real LangGraph pipeline stage, e.g. "SousChef 2/3
    cooking…") and, once terminal, either `result` (the full meal plan,
    including persisted `recipe_id`s) or `error`.

    Jobs are held in memory by the API process: they are lost on restart, and
    an unknown or evicted `job_id` is a 404.
    """
    job = job_registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Recipe generation job {job_id} not found"
        )

    return _job_status(job)


@router.get("/{recipe_id}",
            response_model=RecipeInfo,
            responses={
                404: {"model": ErrorResponse, "description": "Recipe not found"},
                500: {"model": ErrorResponse, "description": "Internal server error"}
            })
async def get_recipe(recipe_id: int) -> RecipeInfo:
    """
    Get recipe by ID.

    Retrieve full recipe details including ingredients, instructions,
    cost breakdown, and nutritional information.
    """
    try:
        recipe = db_service.get_recipe(recipe_id)

        if not recipe:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe with ID {recipe_id} not found"
            )

        return _recipe_row_to_info(recipe)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching recipe {recipe_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch recipe"
        )


@router.get("/user/{user_id}",
            response_model=list[RecipeInfo],
            responses={
                404: {"model": ErrorResponse, "description": "User not found"},
                500: {"model": ErrorResponse, "description": "Internal server error"}
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
    try:
        user = UserService.get_user_by_id(user_id)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ID {user_id} not found"
            )

        recipes = db_service.get_user_recipes(user_id, limit)
        return [_recipe_row_to_info(recipe) for recipe in recipes]

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching recipes for user {user_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch user recipes"
        )
