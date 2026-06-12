import pytest
from app.agents.graph import create_recipe_generation_graph
from app.agents.state import RecipeGenerationState

# Full end-to-end run: needs live Ollama models and the real database
pytestmark = [pytest.mark.llm, pytest.mark.db]

def test_basic_workflow():
    """Test basic recipe generation workflow."""

    # Mock initial state
    initial_state = RecipeGenerationState(
        user_id=999,
        postal_code="M5V3A8",
        budget=50.0,
        household_size=2,
        dietary_restrictions=["vegetarian"],
        num_meals=3,
        preferences={},
        # Initialize all other required fields
        available_deals=[],
        deal_index={},
        ingredient_groups=[],
        ingredient_reuse_map={},
        target_ingredients_per_group=0,
        generated_recipes={},
        sous_chef_assignments={},
        validation_results={},
        approved_recipe_ids=[],
        rejected_recipe_ids=[],
        iteration_count=0,
        max_iterations=2,
        retry_strategy="reassign_chef",
        recipes_pending_retry={},
        total_cost=0.0,
        cost_per_meal=0.0,
        estimated_savings=0.0,
        budget_remaining=50.0,
        mlflow_run_id="",
        agent_call_log=[],
        status="initializing",
        errors=[],
        warnings=[]
    )

    graph = create_recipe_generation_graph()

    # Run workflow
    final_state = graph.invoke(initial_state)

    # Assertions
    assert final_state["status"] in ["completed", "failed"]
    assert len(final_state["agent_call_log"]) > 0
    assert final_state["iteration_count"] <= final_state["max_iterations"]

    if final_state["status"] == "completed":
        assert len(final_state["approved_recipe_ids"]) > 0
        assert final_state["total_cost"] <= initial_state["budget"]

def test_rejection_retry():
    """Test that rejection/retry loop works."""
    # This would require mocking LLM responses
    pass

def test_parallel_execution():
    """Test that 3 SousChefs execute in parallel."""
    # Verify timing shows parallelism
    pass
