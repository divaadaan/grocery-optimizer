"""
Example: DSPy 3.0 Integration with Grocery Optimizer

This script demonstrates how to use the DSPy-powered multi-agent system
for recipe generation with declarative language model programming.

Usage:
    python example_dspy_usage.py
"""

import json
from app.agents.graph_dspy import run_recipe_generation_dspy, create_recipe_generation_graph_dspy
from app.agents.dspy_modules import create_dspy_agents
from app.agents.dspy_config import initialize_dspy
from app.agents.state import RecipeGenerationState


def example_1_full_workflow():
    """
    Example 1: Run the complete DSPy-powered workflow.
    """
    print("\n" + "="*70)
    print("EXAMPLE 1: Complete DSPy-Powered Workflow")
    print("="*70 + "\n")

    # Run the workflow
    final_state = run_recipe_generation_dspy(
        user_id=1,
        postal_code="12345",
        budget=100.0,
        household_size=4,
        dietary_restrictions=["vegetarian", "nut-free"],
        num_meals=7,
        preferences={
            "cuisine": "Mediterranean",
            "spice_level": "mild",
            "cooking_time": "under_30_min"
        }
    )

    # Display results
    print(f"\n{'='*70}")
    print("RESULTS")
    print(f"{'='*70}\n")

    print(f"Status: {final_state['status']}")
    print(f"Total Recipes: {len(final_state['generated_recipes'])}")
    print(f"Approved: {len(final_state['approved_recipe_ids'])}")
    print(f"Rejected: {len(final_state['rejected_recipe_ids'])}")
    print(f"Total Cost: ${final_state['total_cost']:.2f}")
    print(f"Cost per Meal: ${final_state['cost_per_meal']:.2f}")
    print(f"Budget Remaining: ${final_state['budget_remaining']:.2f}")
    print(f"Iterations: {final_state['iteration_count']}")

    if final_state['errors']:
        print(f"\nErrors: {final_state['errors']}")

    if final_state['warnings']:
        print(f"\nWarnings: {final_state['warnings']}")

    # Display agent reasoning
    print(f"\n{'='*70}")
    print("AGENT REASONING")
    print(f"{'='*70}\n")

    for log_entry in final_state['agent_call_log']:
        print(f"\n[{log_entry['agent']}] {log_entry['action']}")
        if 'reasoning' in log_entry:
            print(f"  Reasoning: {log_entry['reasoning'][:200]}...")
        if 'creative_notes' in log_entry:
            print(f"  Creative: {log_entry['creative_notes'][:200]}...")
        if 'overall_assessment' in log_entry:
            print(f"  Assessment: {log_entry['overall_assessment'][:200]}...")
        print(f"  Time: {log_entry.get('elapsed_time', 0):.2f}s")

    # Display sample recipes
    if final_state['approved_recipe_ids']:
        print(f"\n{'='*70}")
        print("SAMPLE APPROVED RECIPES")
        print(f"{'='*70}\n")

        for i, recipe_id in enumerate(final_state['approved_recipe_ids'][:2], 1):
            recipe = final_state['generated_recipes'][recipe_id]
            validation = final_state['validation_results'][recipe_id]

            print(f"\nRecipe {i}: {recipe['name']}")
            print(f"  Cost: ${recipe['total_cost']:.2f}")
            print(f"  Servings: {recipe['servings']}")
            print(f"  Prep Time: {recipe['estimated_prep_time']} min")
            print(f"  Health Score: {validation['health_score']}/100")
            print(f"  Ingredients: {', '.join([ing['name'] for ing in recipe['ingredients'][:5]])}...")


def example_2_individual_modules():
    """
    Example 2: Use individual DSPy modules directly.
    """
    print("\n" + "="*70)
    print("EXAMPLE 2: Using Individual DSPy Modules")
    print("="*70 + "\n")

    # Create DSPy agents
    agents = create_dspy_agents()
    chef = agents["chef"]
    sous_chef = agents["sous_chef"]
    nutritionist = agents["nutritionist"]

    # Sample data
    sample_deals = [
        {"product_name": "Tomatoes", "price": 2.99, "unit": "lb", "quantity": 1.5, "store_id": 1},
        {"product_name": "Pasta", "price": 1.49, "unit": "box", "quantity": 1, "store_id": 1},
        {"product_name": "Olive Oil", "price": 7.99, "unit": "bottle", "quantity": 1, "store_id": 2},
        {"product_name": "Garlic", "price": 0.99, "unit": "bulb", "quantity": 2, "store_id": 1},
        {"product_name": "Basil", "price": 2.49, "unit": "bunch", "quantity": 1, "store_id": 2},
        {"product_name": "Mozzarella", "price": 4.99, "unit": "lb", "quantity": 0.5, "store_id": 1},
        {"product_name": "Bell Peppers", "price": 3.49, "unit": "lb", "quantity": 1, "store_id": 2},
        {"product_name": "Zucchini", "price": 2.79, "unit": "lb", "quantity": 1, "store_id": 1},
        {"product_name": "Onions", "price": 1.99, "unit": "lb", "quantity": 2, "store_id": 2},
    ]

    # Step 1: Chef plans ingredient groups
    print("\n[STEP 1] Chef Planning Ingredient Groups with DSPy ChainOfThought\n")
    initialize_dspy("chef")

    groups, reuse_map, reasoning = chef.plan_ingredients(
        available_deals=sample_deals,
        budget=50.0,
        num_meals=3,
        household_size=4,
        dietary_restrictions=["vegetarian"],
        preferences={"cuisine": "Italian"},
    )

    print(f"Reasoning: {reasoning}\n")
    print(f"Created {len(groups)} ingredient groups:")
    for i, group in enumerate(groups, 1):
        total_cost = sum(item['price'] * item['quantity'] for item in group)
        print(f"  Group {i}: {len(group)} ingredients, ${total_cost:.2f}")

    print(f"\nIngredient Reuse Map: {reuse_map}")

    # Step 2: SousChef generates recipes
    print("\n[STEP 2] SousChef Generating Recipes with DSPy ChainOfThought\n")
    initialize_dspy("sous_chef")

    recipes, usage, creative_notes = sous_chef.generate_recipes(
        chef_id="sous_chef_demo",
        ingredient_group=groups[0],
        target_recipe_count=1,
        household_size=4,
        dietary_restrictions=["vegetarian"],
        preferences={"cuisine": "Italian"},
    )

    print(f"Creative Notes: {creative_notes}\n")
    print(f"Generated {len(recipes)} recipe(s):")
    for recipe in recipes:
        print(f"  - {recipe['name']} (${recipe['total_cost']:.2f}, {recipe['servings']} servings)")

    # Step 3: Nutritionist validates
    print("\n[STEP 3] Nutritionist Validating with DSPy ChainOfThought\n")
    initialize_dspy("nutritionist")

    validations, assessment, recommendations = nutritionist.validate_recipes(
        recipes=recipes,
        dietary_restrictions=["vegetarian"],
        household_size=4,
    )

    print(f"Overall Assessment: {assessment}\n")
    print(f"Recommendations: {recommendations}\n")

    for validation in validations:
        status = "✓ APPROVED" if validation['approved'] else "✗ REJECTED"
        print(f"  {status}: {validation['recipe_id']}")
        print(f"    Health Score: {validation['health_score']}/100")
        print(f"    Feedback: {validation['feedback']}")
        if validation['nutrition_facts']:
            print(f"    Nutrition: {validation['nutrition_facts']}")


def example_3_parallel_generation():
    """
    Example 3: Parallel recipe generation with DSPy batch method.
    """
    print("\n" + "="*70)
    print("EXAMPLE 3: Parallel Recipe Generation with DSPy Batch")
    print("="*70 + "\n")

    # Initialize
    agents = create_dspy_agents()
    sous_chef = agents["sous_chef"]
    initialize_dspy("sous_chef")

    # Prepare 3 ingredient groups
    groups = [
        [
            {"product_name": "Chicken", "price": 8.99, "unit": "lb", "quantity": 2},
            {"product_name": "Rice", "price": 2.49, "unit": "box", "quantity": 1},
            {"product_name": "Broccoli", "price": 1.99, "unit": "lb", "quantity": 1},
        ],
        [
            {"product_name": "Ground Beef", "price": 7.99, "unit": "lb", "quantity": 1.5},
            {"product_name": "Pasta", "price": 1.49, "unit": "box", "quantity": 1},
            {"product_name": "Tomatoes", "price": 2.99, "unit": "lb", "quantity": 1},
        ],
        [
            {"product_name": "Salmon", "price": 12.99, "unit": "lb", "quantity": 1},
            {"product_name": "Potatoes", "price": 3.49, "unit": "lb", "quantity": 2},
            {"product_name": "Asparagus", "price": 3.99, "unit": "bunch", "quantity": 1},
        ],
    ]

    # Configure batch generation
    chef_configs = [
        {
            "chef_id": f"sous_chef_{i+1}",
            "ingredient_group": group,
            "target_recipe_count": 2,
            "household_size": 4,
            "dietary_restrictions": [],
            "preferences": {"cuisine": "American"},
        }
        for i, group in enumerate(groups)
    ]

    print("Generating recipes in parallel for 3 sous chefs...\n")

    # Execute in parallel
    results = sous_chef.batch_generate_recipes(chef_configs)

    # Display results
    for i, result in enumerate(results, 1):
        print(f"\n[SousChef {i}] Generated {len(result['recipes'])} recipes")
        print(f"  Creative Notes: {result['creative_notes'][:150]}...")
        for recipe in result['recipes']:
            print(f"    - {recipe['name']} (${recipe['total_cost']:.2f})")


def example_4_comparison():
    """
    Example 4: Compare DSPy vs Original Implementation.
    """
    print("\n" + "="*70)
    print("EXAMPLE 4: DSPy vs Original Implementation Comparison")
    print("="*70 + "\n")

    print("┌─────────────────────────┬──────────────────┬──────────────────┐")
    print("│ Feature                 │ Original         │ DSPy Integration │")
    print("├─────────────────────────┼──────────────────┼──────────────────┤")
    print("│ Prompt Engineering      │ Manual           │ Declarative      │")
    print("│ Reasoning Mode          │ Basic            │ ChainOfThought   │")
    print("│ Feedback Handling       │ Manual           │ Refine Module    │")
    print("│ Automatic Optimization  │ No               │ Yes (MIPROv2)    │")
    print("│ Type Safety             │ Limited          │ Strong           │")
    print("│ JSON Handling           │ Manual parsing   │ Automatic        │")
    print("│ Parallel Execution      │ LangGraph Send   │ DSPy batch()     │")
    print("│ State Management        │ LangGraph        │ LangGraph        │")
    print("│ Model Flexibility       │ High             │ High             │")
    print("│ Learning Capability     │ No               │ Yes              │")
    print("└─────────────────────────┴──────────────────┴──────────────────┘")

    print("\n📊 Key Advantages of DSPy Integration:")
    print("  1. Declarative signatures reduce prompt engineering effort")
    print("  2. ChainOfThought improves reasoning quality")
    print("  3. Automatic optimization with training data")
    print("  4. Better error handling and type safety")
    print("  5. Composable modules for reusability")

    print("\n🔄 Migration Strategy:")
    print("  - Run both implementations side-by-side")
    print("  - A/B test with real users")
    print("  - Collect training data for optimization")
    print("  - Gradually transition to DSPy")


if __name__ == "__main__":
    import sys

    print("\n" + "="*70)
    print("DSPy 3.0 Integration Examples for Grocery Optimizer")
    print("="*70)

    print("\nAvailable Examples:")
    print("  1. Complete DSPy-powered workflow")
    print("  2. Individual DSPy modules")
    print("  3. Parallel recipe generation")
    print("  4. DSPy vs Original comparison")
    print("  all. Run all examples")

    if len(sys.argv) > 1:
        choice = sys.argv[1]
    else:
        choice = input("\nSelect example (1-4, all): ").strip()

    examples = {
        "1": example_1_full_workflow,
        "2": example_2_individual_modules,
        "3": example_3_parallel_generation,
        "4": example_4_comparison,
    }

    if choice == "all":
        for func in examples.values():
            func()
    elif choice in examples:
        examples[choice]()
    else:
        print("Invalid choice. Run with argument 1-4 or 'all'")

    print("\n" + "="*70)
    print("Examples Complete!")
    print("="*70 + "\n")
