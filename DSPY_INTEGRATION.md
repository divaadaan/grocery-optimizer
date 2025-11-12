# DSPy 3.0 Integration Guide

## Overview

This document describes the DSPy 3.0 integration with the Grocery Optimizer multi-agent system. The integration combines:

- **DSPy 3.0**: Declarative language model programming with automatic optimization
- **LangGraph**: Stateful workflow orchestration with conditional routing

## Architecture Comparison

### Original LangGraph Implementation

```
LangGraph StateGraph
    ↓
LangChain ChatOllama (manual prompts)
    ↓
Raw JSON parsing and error handling
```

### DSPy 3.0 Integration

```
LangGraph StateGraph (state management)
    ↓
DSPy Modules (ChainOfThought, Refine)
    ↓
DSPy Signatures (declarative I/O contracts)
    ↓
Ollama LLMs (same models)
    ↓
Automatic JSON handling & optimization
```

## Key Benefits

### 1. Declarative Programming
- Define **what** you want, not **how** to prompt
- Signatures specify input/output contracts
- No manual prompt engineering required

### 2. Automatic Optimization
- DSPy optimizers can improve prompts automatically
- Use `dspy.MIPROv2`, `dspy.GEPA`, or other optimizers
- Optimization tracks metrics and improves over time

### 3. Better Reasoning
- `ChainOfThought`: Step-by-step reasoning before outputs
- `Refine`: Iterative improvement with feedback
- `ReAct`: Agent-like tool usage (future capability)

### 4. Type Safety & Validation
- Structured input/output with field descriptions
- Automatic JSON parsing and validation
- Better error handling

### 5. Composability
- Modules can be composed hierarchically
- Reusable components across different workflows
- Easy to test individual modules

## File Structure

```
app/agents/
├── dspy_signatures.py           # Signature definitions (I/O contracts)
├── dspy_modules.py              # DSPy Module implementations
├── dspy_config.py               # LM configuration for Ollama
├── dspy_langgraph_integration.py # LangGraph node wrappers
├── graph_dspy.py                # DSPy-powered StateGraph
└── state.py                     # Shared state definition (unchanged)
```

## Usage

### Basic Usage

```python
from app.agents.graph_dspy import run_recipe_generation_dspy

# Run the DSPy-powered workflow
final_state = run_recipe_generation_dspy(
    user_id=123,
    postal_code="12345",
    budget=100.0,
    household_size=4,
    dietary_restrictions=["vegetarian", "gluten-free"],
    num_meals=7,
    preferences={"cuisine": "Italian", "spice_level": "mild"}
)

print(f"Generated {len(final_state['approved_recipe_ids'])} recipes")
print(f"Total cost: ${final_state['total_cost']:.2f}")
```

### Advanced: Using Individual DSPy Modules

```python
from app.agents.dspy_modules import ChefOrchestratorDSPy, SousChefDSPy, NutritionistDSPy
from app.agents.dspy_config import initialize_dspy

# Initialize DSPy with Chef model
initialize_dspy("chef")

# Create Chef module
chef = ChefOrchestratorDSPy()

# Plan ingredient groups
groups, reuse_map, reasoning = chef.plan_ingredients(
    available_deals=deals,
    budget=100.0,
    num_meals=7,
    household_size=4,
    dietary_restrictions=["vegetarian"],
    preferences={"cuisine": "Italian"}
)

print(f"Reasoning: {reasoning}")
print(f"Created {len(groups)} ingredient groups")
```

### Parallel Recipe Generation

```python
from app.agents.dspy_modules import SousChefDSPy
from app.agents.dspy_config import initialize_dspy

# Initialize with SousChef model
initialize_dspy("sous_chef")

# Create SousChef module
sous_chef = SousChefDSPy()

# Prepare configurations for 3 parallel chefs
chef_configs = [
    {
        "chef_id": "sous_chef_1",
        "ingredient_group": group1,
        "target_recipe_count": 3,
        "household_size": 4,
        "dietary_restrictions": ["vegetarian"],
        "preferences": {"cuisine": "Italian"}
    },
    # ... config for chef 2 and 3
]

# Generate in parallel using DSPy's batch method
results = sous_chef.batch_generate_recipes(chef_configs)

for i, result in enumerate(results):
    print(f"Chef {i+1} generated {len(result['recipes'])} recipes")
    print(f"Creative notes: {result['creative_notes']}")
```

## DSPy Signatures

### Chef Orchestrator

#### IngredientPlanning
Plans optimal ingredient groups for sous chefs.

**Inputs:**
- `available_deals`: JSON array of grocery deals
- `budget`: Total budget in dollars
- `num_meals`: Number of meals to generate
- `household_size`: Number of people
- `dietary_restrictions`: Comma-separated restrictions
- `preferences`: User preferences JSON

**Outputs:**
- `ingredient_groups`: JSON array of 3 groups
- `ingredient_reuse_map`: Reuse count mapping
- `reasoning`: Explanation of decisions

#### RejectionHandling
Analyzes rejected recipes and determines retry strategy.

**Inputs:**
- `rejected_recipes`: JSON array of rejected recipes
- `validation_feedback`: Validation results with feedback
- `current_iteration`: Current iteration number
- `max_iterations`: Maximum allowed iterations
- `available_deals`: Remaining deals

**Outputs:**
- `retry_strategy`: "reassign_chef" or "new_ingredients"
- `retry_assignments`: Recipe-to-chef/ingredient mappings
- `reasoning`: Strategy explanation

### Sous Chef

#### RecipeGeneration
Generates creative recipes from ingredients.

**Inputs:**
- `chef_id`: Chef identifier
- `ingredient_group`: Assigned ingredients JSON
- `target_recipe_count`: Number of recipes
- `household_size`: Servings needed
- `dietary_restrictions`: Restrictions to respect
- `preferences`: Cuisine and meal preferences

**Outputs:**
- `recipes`: Complete recipes array
- `ingredient_usage`: Usage mapping
- `creative_notes`: Creative decisions

#### RecipeRegeneration
Regenerates recipe based on feedback.

**Inputs:**
- `chef_id`: Chef identifier
- `original_recipe`: Original rejected recipe
- `validation_feedback`: Specific improvement feedback
- `ingredient_group`: Available ingredients
- `household_size`: Servings needed
- `dietary_restrictions`: Restrictions

**Outputs:**
- `improved_recipe`: Regenerated recipe
- `improvements_made`: Change explanations

### Nutritionist

#### RecipeValidation
Validates recipes comprehensively.

**Inputs:**
- `recipes`: Recipes to validate
- `dietary_restrictions`: Restrictions to enforce
- `household_size`: People per household

**Outputs:**
- `validation_results`: Per-recipe validation
- `overall_assessment`: Meal plan summary
- `recommendations`: Improvement suggestions

## Configuration

### Model Configuration

Edit `app/agents/dspy_config.py` to customize models:

```python
class DSPyConfig:
    # Change models
    CHEF_MODEL = "smollm:1.7b"
    SOUS_CHEF_MODEL = "smollm:360m"
    NUTRITIONIST_MODEL = "smollm:360m"

    # Adjust temperatures
    CHEF_TEMPERATURE = 0.7
    SOUS_CHEF_TEMPERATURE = 0.8
    NUTRITIONIST_TEMPERATURE = 0.3

    # Ollama URL
    OLLAMA_BASE_URL = "http://localhost:11434"
```

### DSPy Settings

```python
import dspy
from app.agents.dspy_config import DSPyConfig

# Configure specific agent
lm = DSPyConfig.configure_chef_lm()
dspy.settings.configure(lm=lm)

# Or configure all agents at once
lms = DSPyConfig.setup_all_agents()
```

## Optimization (Advanced)

DSPy's killer feature is **automatic prompt optimization**. Here's how to use it:

### 1. Define Training Data

```python
training_examples = [
    dspy.Example(
        available_deals=json.dumps(deals),
        budget=100.0,
        num_meals=7,
        household_size=4,
        dietary_restrictions="vegetarian",
        preferences="{}",
        # Expected outputs
        ingredient_groups=json.dumps(expected_groups),
    ).with_inputs("available_deals", "budget", "num_meals", "household_size", "dietary_restrictions", "preferences")
    for deals, expected_groups in training_data
]
```

### 2. Define Metric

```python
def planning_quality_metric(example, prediction, trace=None):
    """
    Evaluate ingredient planning quality.
    Returns score between 0 and 1.
    """
    # Parse outputs
    groups = json.loads(prediction.ingredient_groups)

    # Check balance
    group_costs = [sum(item['price'] for item in g) for g in groups]
    cost_variance = np.var(group_costs)

    # Check reuse
    reuse_map = json.loads(prediction.ingredient_reuse_map)
    reuse_score = sum(reuse_map.values()) / len(reuse_map)

    # Combined score
    return 0.7 * (1 - cost_variance/100) + 0.3 * min(reuse_score, 1.0)
```

### 3. Run Optimizer

```python
from dspy.teleprompt import MIPROv2

# Initialize optimizer
optimizer = MIPROv2(
    metric=planning_quality_metric,
    num_candidates=10,
    init_temperature=1.0,
)

# Optimize the Chef's ingredient planner
optimized_chef = optimizer.compile(
    dspy_chef.ingredient_planner,
    trainset=training_examples[:50],
    valset=training_examples[50:],
)

# Replace with optimized version
dspy_chef.ingredient_planner = optimized_chef
```

### 4. Save & Load Optimized Module

```python
# Save optimized module
dspy_chef.save("chef_optimized.json")

# Load in production
dspy_chef = ChefOrchestratorDSPy()
dspy_chef.load("chef_optimized.json")
```

## Integration with FastAPI

Update your FastAPI route to use the DSPy version:

```python
from app.agents.graph_dspy import run_recipe_generation_dspy

@router.post("/recipes/generate-dspy")
async def generate_recipes_dspy(request: RecipeGenerationRequest) -> RecipeGenerationResponse:
    """Generate recipes using DSPy-powered agents."""

    # Validate user exists
    user = db_service.get_user(request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Run DSPy workflow
    final_state = run_recipe_generation_dspy(
        user_id=request.user_id,
        postal_code=request.postal_code,
        budget=request.budget,
        household_size=request.household_size,
        dietary_restrictions=request.dietary_restrictions,
        num_meals=request.num_meals,
        preferences=request.preferences,
    )

    # Return response
    return RecipeGenerationResponse(
        status=final_state["status"],
        recipes=list(final_state["generated_recipes"].values()),
        total_cost=final_state["total_cost"],
        validation_results=list(final_state["validation_results"].values()),
        errors=final_state["errors"],
        warnings=final_state["warnings"],
    )
```

## Testing

### Unit Test Individual Modules

```python
import pytest
from app.agents.dspy_modules import ChefOrchestratorDSPy
from app.agents.dspy_config import initialize_dspy

@pytest.fixture
def chef_module():
    initialize_dspy("chef")
    return ChefOrchestratorDSPy()

def test_ingredient_planning(chef_module):
    deals = [
        {"product_name": "Tomatoes", "price": 2.99},
        {"product_name": "Pasta", "price": 1.49},
        # ... more deals
    ]

    groups, reuse_map, reasoning = chef_module.plan_ingredients(
        available_deals=deals,
        budget=50.0,
        num_meals=3,
        household_size=2,
        dietary_restrictions=["vegetarian"],
        preferences={},
    )

    assert len(groups) == 3
    assert isinstance(reuse_map, dict)
    assert len(reasoning) > 0
```

### Integration Test Full Workflow

```python
def test_full_dspy_workflow():
    from app.agents.graph_dspy import run_recipe_generation_dspy

    final_state = run_recipe_generation_dspy(
        user_id=1,
        postal_code="12345",
        budget=100.0,
        household_size=4,
        dietary_restrictions=[],
        num_meals=5,
        preferences={},
    )

    assert final_state["status"] in ["completed", "failed"]
    assert len(final_state["generated_recipes"]) > 0
    assert final_state["total_cost"] <= 100.0
```

## Performance Comparison

| Metric | Original LangGraph | DSPy Integration |
|--------|-------------------|------------------|
| Lines of Code | ~800 | ~1200 (more modular) |
| Prompt Engineering | Manual | Declarative |
| Optimization | Manual | Automatic |
| Type Safety | Limited | Strong |
| Reasoning Quality | Good | Better (CoT) |
| Parallel Execution | ✓ | ✓ (batch method) |
| State Management | ✓ | ✓ |

## Migration Path

### Option 1: Side-by-side (Recommended)

Keep both implementations and A/B test:

```python
# Original
from app.agents.graph import create_recipe_generation_graph

# DSPy version
from app.agents.graph_dspy import create_recipe_generation_graph_dspy

# Choose based on feature flag
if use_dspy:
    graph = create_recipe_generation_graph_dspy()
else:
    graph = create_recipe_generation_graph()
```

### Option 2: Gradual Migration

Replace agents one at a time:

1. Start with Nutritionist (simplest)
2. Add SousChef (parallel execution)
3. Complete with Chef (most complex)

### Option 3: Full Replacement

Switch entirely to DSPy:

1. Test thoroughly with existing test suite
2. Run optimization to improve prompts
3. Deploy and monitor metrics
4. Remove old implementation

## Troubleshooting

### Issue: JSON Parsing Errors

DSPy handles JSON automatically, but if you see errors:

```python
# Ensure format="json" in LM config
lm = dspy.LM(
    model="ollama/smollm:1.7b",
    format="json",  # Important!
)
```

### Issue: Ollama Connection Failed

Check Ollama is running:

```bash
curl http://localhost:11434/api/tags
```

Set custom URL in environment:

```bash
export OLLAMA_BASE_URL="http://your-ollama-host:11434"
```

### Issue: Module Not Producing Expected Output

Enable DSPy debugging:

```python
import dspy
dspy.settings.configure(trace=True)

# Run your module - you'll see full traces
result = chef.plan_ingredients(...)
```

## Next Steps

1. **Run the Example**: Test with `python -m app.agents.dspy_config`
2. **Try Optimization**: Collect training data and run MIPROv2
3. **Monitor Performance**: Compare with original implementation
4. **Iterate**: Refine signatures and modules based on results

## Resources

- [DSPy Documentation](https://dspy.ai/)
- [DSPy GitHub](https://github.com/stanfordnlp/dspy)
- [LangGraph Documentation](https://python.langchain.com/docs/langgraph)
- [MLflow Integration](https://mlflow.org/docs/latest/index.html)

## Support

For questions or issues with this integration:
1. Check the troubleshooting section above
2. Review DSPy documentation
3. Open an issue in the project repository
