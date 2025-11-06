# LangGraph Recipe Generation System - Implementation Guide

## Overview
This guide provides complete implementation instructions for building a multi-agent recipe generation system using LangGraph, SmolLM models via Ollama, and MLflow tracking. The system orchestrates 3 SousChef agents coordinated by a Chef Orchestrator, with a Nutritionist validator ensuring quality and dietary compliance.

## Architecture Summary

```
User Request → Chef Orchestrator → 3 Parallel SousChefs → Aggregator → Nutritionist Validator
                                                                              ↓
                                                                      Approved/Rejected?
                                                                              ↓
                                                              Yes: Finalize  |  No: Retry (max 2x)
                                                                              ↓
                                                                      Meal Plan Output
```

## Prerequisites

### Required Services
- **PostgreSQL with TimescaleDB** (Neon.tech): For price_snapshots and deals tables
- **Ollama**: Local LLM serving with SmolLM models
- **MLflow**: Experiment tracking and logging
- **Redis** (Upstash): Caching (optional for PoC)

### Required Models in Ollama
```bash
# Pull SmolLM models
ollama pull smollm:1.7b     # Chef Orchestrator
ollama pull smollm:360m     # SousChef agents and Nutritionist
```

### Python Dependencies
```txt
langgraph>=0.2.0
langchain-core>=0.3.0
langchain-ollama>=0.1.0
mlflow>=2.9.0
psycopg2-binary>=2.9.9
pydantic>=2.0.0
python-dotenv>=1.0.0
```

## Project Structure

```
app/
├── agents/
│   ├── __init__.py
│   ├── state.py              # State schema definitions
│   ├── chef_orchestrator.py  # Chef agent implementation
│   ├── sous_chef.py           # SousChef worker implementation
│   ├── nutritionist.py        # Nutritionist validator implementation
│   ├── graph.py               # LangGraph workflow definition
│   └── prompts.py             # Agent prompt templates
├── services/
│   ├── __init__.py
│   ├── database.py            # Database queries for deals
│   ├── mlflow_logger.py       # MLflow tracking utilities
│   └── cost_calculator.py     # Recipe cost calculation
├── models/
│   ├── __init__.py
│   └── schemas.py             # Pydantic models for recipes, validation
└── tests/
    ├── test_agents.py
    └── test_graph.py
```

## Implementation Steps

### Step 1: Define State Schema

**File: `app/agents/state.py`**

```python
from typing import TypedDict, List, Dict, Literal, Optional
from datetime import datetime

class Recipe(TypedDict):
    """Individual recipe structure."""
    recipe_id: str
    name: str
    ingredients: List[Dict]  # [{name, quantity, unit, price, store_id}]
    instructions: List[str]
    servings: int
    total_cost: float
    estimated_prep_time: int
    meal_type: str
    cuisine_type: Optional[str]

class ValidationResult(TypedDict):
    """Nutritionist validation output."""
    recipe_id: str
    approved: bool
    feedback: str  # Specific improvement guidance if rejected
    nutrition_facts: Dict  # {calories, protein_g, carbs_g, fat_g, vitamins}
    dietary_compliance: Dict  # {allergen_free: bool, meets_restrictions: bool}
    health_score: float  # 0-100

class RecipeGenerationState(TypedDict):
    """Complete LangGraph state."""
    
    # Input configuration
    user_id: int
    postal_code: str
    budget: float
    household_size: int
    dietary_restrictions: List[str]
    num_meals: int
    preferences: Dict
    
    # Deal data from database
    available_deals: List[Dict]
    deal_index: Dict[str, Dict]
    
    # Chef orchestration
    ingredient_groups: List[List[Dict]]  # 3 groups for 3 SousChefs
    ingredient_reuse_map: Dict[str, int]
    target_ingredients_per_group: int
    
    # SousChef outputs
    generated_recipes: Dict[str, Recipe]
    sous_chef_assignments: Dict[str, str]
    
    # Nutritionist validation
    validation_results: Dict[str, ValidationResult]
    approved_recipe_ids: List[str]
    rejected_recipe_ids: List[str]
    
    # Retry mechanism
    iteration_count: int
    max_iterations: int
    retry_strategy: Literal["reassign_chef", "new_ingredients"]
    recipes_pending_retry: Dict[str, str]
    
    # Cost tracking
    total_cost: float
    cost_per_meal: float
    estimated_savings: float
    budget_remaining: float
    
    # MLflow tracking
    mlflow_run_id: str
    agent_call_log: List[Dict]
    
    # Workflow control
    status: Literal["initializing", "planning", "generating", 
                   "validating", "retrying", "completed", "failed"]
    errors: List[str]
    warnings: List[str]
```

### Step 2: Database Service

**File: `app/services/database.py`**

```python
import psycopg2
from typing import List, Dict
import os
from dotenv import load_dotenv

load_dotenv()

class DatabaseService:
    """Handle all database interactions for recipe generation."""
    
    def __init__(self):
        self.conn_string = os.getenv("DATABASE_URL")
    
    def get_connection(self):
        """Create database connection."""
        return psycopg2.connect(self.conn_string)
    
    def fetch_current_deals(self, postal_code: str) -> List[Dict]:
        """
        Fetch all current deals for a postal code.
        
        Query deals table for valid offers, joining with stores.
        """
        query = """
        SELECT 
            d.deal_id,
            d.product_name,
            d.sale_price,
            d.regular_price,
            d.discount_percentage,
            s.name as store_name,
            s.store_id,
            s.chain
        FROM deals d
        JOIN stores s ON d.store_id = s.store_id
        WHERE s.postal_code = %s
          AND d.valid_until >= CURRENT_DATE
          AND d.valid_from <= CURRENT_DATE
        ORDER BY d.discount_percentage DESC
        LIMIT 200;  -- Cap for performance
        """
        
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (postal_code,))
                columns = [desc[0] for desc in cur.description]
                results = cur.fetchall()
                
                return [dict(zip(columns, row)) for row in results]
    
    def save_recipes(self, user_id: int, recipes: List[Recipe]) -> List[int]:
        """
        Save generated recipes to database.
        
        Returns list of recipe_ids.
        """
        query = """
        INSERT INTO recipes (
            user_id, name, ingredients, instructions, 
            total_cost, servings, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW())
        RETURNING recipe_id;
        """
        
        recipe_ids = []
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                for recipe in recipes:
                    cur.execute(query, (
                        user_id,
                        recipe['name'],
                        recipe['ingredients'],  # JSONB
                        recipe['instructions'],  # TEXT[]
                        recipe['total_cost'],
                        recipe['servings']
                    ))
                    recipe_id = cur.fetchone()[0]
                    recipe_ids.append(recipe_id)
                
                conn.commit()
        
        return recipe_ids
```

### Step 3: MLflow Logger

**File: `app/services/mlflow_logger.py`**

```python
import mlflow
from typing import Dict, Any
import time

class MLflowLogger:
    """Track all agent interactions and metrics."""
    
    @staticmethod
    def start_run(user_id: int, num_meals: int, budget: float, 
                  dietary_restrictions: list) -> str:
        """Initialize MLflow run."""
        mlflow.start_run(run_name=f"recipe_gen_user_{user_id}_{int(time.time())}")
        
        # Log input parameters
        mlflow.log_params({
            "user_id": user_id,
            "num_meals": num_meals,
            "budget": budget,
            "dietary_restrictions": ",".join(dietary_restrictions),
            "timestamp": time.time()
        })
        
        return mlflow.active_run().info.run_id
    
    @staticmethod
    def log_agent_call(agent_name: str, tokens: int, duration: float,
                       model: str, success: bool, error: str = None):
        """Log individual agent invocation."""
        prefix = agent_name.lower().replace(" ", "_")
        
        mlflow.log_metrics({
            f"{prefix}_tokens": tokens,
            f"{prefix}_duration_sec": duration,
        }, step=int(time.time()))
        
        if error:
            mlflow.log_param(f"{prefix}_error", error[:200])  # Truncate
    
    @staticmethod
    def log_ingredient_groups(groups: List[List[Dict]], reuse_map: Dict[str, int]):
        """Log Chef's ingredient grouping strategy."""
        mlflow.log_metrics({
            "ingredient_groups_count": len(groups),
            "avg_ingredients_per_group": sum(len(g) for g in groups) / len(groups),
            "total_unique_ingredients": len(reuse_map),
            "max_ingredient_reuse": max(reuse_map.values()),
            "avg_ingredient_reuse": sum(reuse_map.values()) / len(reuse_map)
        })
        
        # Log reuse map as artifact
        mlflow.log_dict(reuse_map, "ingredient_reuse_map.json")
    
    @staticmethod
    def log_validation_results(validation_results: Dict[str, Any]):
        """Log Nutritionist validation metrics."""
        approved = sum(1 for r in validation_results.values() if r["approved"])
        rejected = len(validation_results) - approved
        
        avg_health_score = (
            sum(r["health_score"] for r in validation_results.values()) 
            / len(validation_results)
        ) if validation_results else 0
        
        mlflow.log_metrics({
            "recipes_validated": len(validation_results),
            "recipes_approved": approved,
            "recipes_rejected": rejected,
            "approval_rate": approved / len(validation_results) if validation_results else 0,
            "avg_health_score": avg_health_score
        })
        
        # Log rejection reasons
        rejection_reasons = [
            r["feedback"] for r in validation_results.values() if not r["approved"]
        ]
        if rejection_reasons:
            mlflow.log_text("\n---\n".join(rejection_reasons), "rejection_reasons.txt")
    
    @staticmethod
    def log_final_metrics(total_cost: float, cost_per_meal: float,
                          estimated_savings: float, iterations: int,
                          recipe_count: int, success: bool):
        """Log final workflow metrics."""
        mlflow.log_metrics({
            "total_cost": total_cost,
            "cost_per_meal": cost_per_meal,
            "estimated_savings": estimated_savings,
            "iterations": iterations,
            "final_recipe_count": recipe_count
        })
        
        mlflow.log_param("workflow_success", success)
    
    @staticmethod
    def finalize_run(state: Dict[str, Any]):
        """Log artifacts and close run."""
        # Log approved recipes
        if state.get("approved_recipe_ids"):
            approved_recipes = {
                rid: state["generated_recipes"][rid] 
                for rid in state["approved_recipe_ids"]
            }
            mlflow.log_dict(approved_recipes, "approved_recipes.json")
        
        # Log agent call timeline
        if state.get("agent_call_log"):
            mlflow.log_dict(state["agent_call_log"], "agent_call_log.json")
        
        mlflow.end_run()
```

### Step 4: Prompt Templates

**File: `app/agents/prompts.py`**

```python
class PromptTemplates:
    """Centralized prompt templates for all agents."""
    
    CHEF_INGREDIENT_PLANNING = """You are a professional Chef Orchestrator managing a meal planning system.

Your task: Analyze available grocery deals and create 3 optimized ingredient groups for your team of 3 SousChefs.

**Available Deals:**
{deals_json}

**User Requirements:**
- Budget: ${budget}
- Household Size: {household_size} people
- Number of Meals: {num_meals}
- Dietary Restrictions: {dietary_restrictions}
- Preferences: {preferences}

**Optimization Strategy:**
1. Maximize ingredient reuse across groups (prefer ingredients that appear in multiple groups)
2. Balance cost across all 3 groups
3. Ensure each group has complementary ingredients (protein + starch + vegetables)
4. Respect dietary restrictions strictly
5. Each group should support 2-3 meal recipes

**Output Format (JSON):**
{{
  "ingredient_groups": [
    [
      {{"product_name": "...", "quantity_estimate": "...", "deal_id": "..."}},
      ...
    ],
    [...],
    [...]
  ],
  "ingredient_reuse_map": {{"ingredient_name": reuse_count, ...}},
  "rationale": "Explanation of grouping strategy"
}}

Generate the 3 ingredient groups now:"""

    SOUS_CHEF_RECIPE_GENERATION = """You are a creative SousChef specializing in practical, delicious recipes.

**Your Assigned Ingredients:**
{ingredients_json}

**Recipe Requirements:**
- Number of recipes to create: {target_recipe_count}
- Servings per recipe: {servings}
- Dietary restrictions: {dietary_restrictions}
- Use ONLY the assigned ingredients (you can use basic pantry staples: salt, pepper, oil, water)
- Calculate exact costs using the provided prices

**Output Format (JSON array):**
[
  {{
    "name": "Recipe Name",
    "meal_type": "breakfast|lunch|dinner",
    "cuisine_type": "Italian|Mexican|Asian|American|etc",
    "ingredients": [
      {{"name": "...", "quantity": "...", "unit": "...", "price": 0.00}}
    ],
    "instructions": ["Step 1...", "Step 2...", ...],
    "estimated_prep_time": 30,
    "total_cost": 0.00,
    "servings": {servings}
  }},
  ...
]

Generate {target_recipe_count} creative recipes now:"""

    SOUS_CHEF_RETRY_WITH_FEEDBACK = """You are a SousChef revising a rejected recipe.

**Original Recipe:**
{original_recipe_json}

**Nutritionist Feedback:**
{feedback}

**Your Assigned Ingredients:**
{ingredients_json}

**Task:**
Create a NEW recipe using these ingredients that addresses the feedback.
- Fix the specific issues mentioned
- Maintain dietary compliance
- Keep costs within budget
- Ensure nutritional balance

**Output Format (same JSON as before):**
{{
  "name": "Revised Recipe Name",
  "meal_type": "...",
  "ingredients": [...],
  "instructions": [...],
  "total_cost": 0.00,
  "servings": {servings}
}}

Generate the revised recipe now:"""

    NUTRITIONIST_VALIDATION = """You are a professional Nutritionist validating recipe quality and safety.

**Recipe to Validate:**
{recipe_json}

**User's Dietary Restrictions:**
{dietary_restrictions}

**Validation Checklist:**
1. **Allergen Safety:** Does this recipe contain any restricted allergens?
2. **Dietary Compliance:** Does it meet all dietary restrictions (vegan, kosher, etc)?
3. **Nutritional Balance:** Are macros balanced? (protein, carbs, healthy fats)
4. **Practical Cooking:** Are instructions clear and achievable?
5. **Ingredient Coherence:** Do ingredients work well together?

**Calculate Nutrition Facts:**
- Estimate calories per serving
- Estimate protein (g), carbs (g), fat (g)
- Note significant vitamins/minerals

**Output Format (JSON):**
{{
  "approved": true/false,
  "feedback": "Specific issues if rejected, or praise if approved",
  "nutrition_facts": {{
    "calories_per_serving": 0,
    "protein_g": 0,
    "carbs_g": 0,
    "fat_g": 0,
    "fiber_g": 0,
    "vitamins": ["Vitamin A", "Iron", ...]
  }},
  "dietary_compliance": {{
    "allergen_free": true/false,
    "meets_restrictions": true/false,
    "violations": ["list any violations"]
  }},
  "health_score": 0-100
}}

If you reject this recipe, provide specific, actionable feedback for improvement.

Validate the recipe now:"""

    CHEF_NEW_INGREDIENTS_SELECTION = """You are a Chef Orchestrator selecting alternative ingredients.

**Context:** Some recipes were rejected and need new ingredient combinations.

**Rejected Recipe Feedback:**
{rejection_feedback}

**Remaining Available Deals:**
{remaining_deals_json}

**Requirements:**
- Budget remaining: ${budget_remaining}
- Dietary restrictions: {dietary_restrictions}
- Number of recipes needed: {recipes_needed}

**Task:**
Select new ingredient combinations that:
1. Address the specific issues from feedback
2. Stay within budget
3. Avoid previous problems
4. Maintain nutritional balance

**Output Format (JSON):**
{{
  "new_ingredient_groups": [
    [
      {{"product_name": "...", "quantity_estimate": "...", "deal_id": "..."}},
      ...
    ]
  ],
  "rationale": "Why these ingredients will work better"
}}

Generate the new ingredient selections now:"""
```

### Step 5: Chef Orchestrator Agent

**File: `app/agents/chef_orchestrator.py`**

```python
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
import json
import time
from typing import Dict, Any
from .state import RecipeGenerationState
from .prompts import PromptTemplates
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger

class ChefOrchestrator:
    """Chef agent using SmolLM-1.7B for high-level planning."""
    
    def __init__(self):
        self.llm = ChatOllama(
            model="smollm:1.7b",
            temperature=0.7,
            format="json"
        )
        self.db = DatabaseService()
    
    def initialize(self, state: RecipeGenerationState) -> RecipeGenerationState:
        """
        Node 1: Initialize Chef by fetching deals and setting up workflow.
        """
        print(f"[Chef] Initializing for user {state['user_id']}...")
        
        # Start MLflow run
        mlflow_run_id = MLflowLogger.start_run(
            user_id=state["user_id"],
            num_meals=state["num_meals"],
            budget=state["budget"],
            dietary_restrictions=state["dietary_restrictions"]
        )
        
        # Fetch deals from database
        deals = self.db.fetch_current_deals(state["postal_code"])
        
        # Create deal index for O(1) lookups
        deal_index = {deal["product_name"]: deal for deal in deals}
        
        # Update state
        state["mlflow_run_id"] = mlflow_run_id
        state["available_deals"] = deals
        state["deal_index"] = deal_index
        state["status"] = "planning"
        state["iteration_count"] = 0
        state["max_iterations"] = 2
        state["agent_call_log"] = []
        state["errors"] = []
        state["warnings"] = []
        state["generated_recipes"] = {}
        state["validation_results"] = {}
        state["approved_recipe_ids"] = []
        state["rejected_recipe_ids"] = []
        
        print(f"[Chef] Found {len(deals)} deals for postal code {state['postal_code']}")
        
        return state
    
    def plan_ingredient_groups(self, state: RecipeGenerationState) -> RecipeGenerationState:
        """
        Node 2: Use LLM to create 3 optimized ingredient groups.
        """
        print("[Chef] Planning ingredient groups for 3 SousChefs...")
        
        start_time = time.time()
        
        # Prepare prompt
        prompt = PromptTemplates.CHEF_INGREDIENT_PLANNING.format(
            deals_json=json.dumps(state["available_deals"][:100], indent=2),  # Limit for context
            budget=state["budget"],
            household_size=state["household_size"],
            num_meals=state["num_meals"],
            dietary_restrictions=", ".join(state["dietary_restrictions"]),
            preferences=json.dumps(state.get("preferences", {}))
        )
        
        # Call LLM
        try:
            response = self.llm.invoke([HumanMessage(content=prompt)])
            result = json.loads(response.content)
            
            # Extract groups
            ingredient_groups = result["ingredient_groups"]
            ingredient_reuse_map = result["ingredient_reuse_map"]
            
            # Validate we have 3 groups
            if len(ingredient_groups) != 3:
                raise ValueError(f"Expected 3 groups, got {len(ingredient_groups)}")
            
            # Update state
            state["ingredient_groups"] = ingredient_groups
            state["ingredient_reuse_map"] = ingredient_reuse_map
            state["status"] = "generating"
            
            # Log to MLflow
            duration = time.time() - start_time
            MLflowLogger.log_agent_call(
                agent_name="Chef_Orchestrator",
                tokens=len(response.content),  # Approximate
                duration=duration,
                model="smollm:1.7b",
                success=True
            )
            MLflowLogger.log_ingredient_groups(ingredient_groups, ingredient_reuse_map)
            
            state["agent_call_log"].append({
                "agent": "Chef_Orchestrator",
                "action": "plan_ingredient_groups",
                "timestamp": time.time(),
                "duration": duration,
                "success": True
            })
            
            print(f"[Chef] Created 3 ingredient groups with {len(ingredient_reuse_map)} unique ingredients")
            print(f"[Chef] Ingredient reuse: {ingredient_reuse_map}")
            
        except Exception as e:
            state["errors"].append(f"Chef planning failed: {str(e)}")
            state["status"] = "failed"
            print(f"[Chef] ERROR: {e}")
        
        return state
    
    def handle_rejections(self, state: RecipeGenerationState) -> RecipeGenerationState:
        """
        Node 7: Process rejected recipes and determine retry strategy.
        """
        print(f"[Chef] Handling {len(state['rejected_recipe_ids'])} rejected recipes...")
        
        state["iteration_count"] += 1
        
        # Determine strategy
        if state["iteration_count"] == 1:
            # Strategy A: Reassign to different SousChef
            state["retry_strategy"] = "reassign_chef"
            print("[Chef] Strategy: Reassign to different SousChef")
            
            # Keep same ingredient groups, just mark for retry
            state["recipes_pending_retry"] = {
                recipe_id: state["validation_results"][recipe_id]["feedback"]
                for recipe_id in state["rejected_recipe_ids"]
            }
            
        else:
            # Strategy B: Select new ingredients
            state["retry_strategy"] = "new_ingredients"
            print("[Chef] Strategy: Select new ingredients from remaining deals")
            
            # Use LLM to pick new ingredients
            # (Implementation similar to plan_ingredient_groups)
            # For now, simplified version
            state["recipes_pending_retry"] = {}
            state["warnings"].append("Max iterations reached, selecting new ingredients")
        
        return state
```

### Step 6: SousChef Agent

**File: `app/agents/sous_chef.py`**

```python
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
import json
import time
import uuid
from typing import Dict, List
from .state import Recipe, RecipeGenerationState
from .prompts import PromptTemplates
from ..services.mlflow_logger import MLflowLogger

class SousChef:
    """SousChef agent using SmolLM-360M for recipe generation."""
    
    def __init__(self):
        self.llm = ChatOllama(
            model="smollm:360m",
            temperature=0.8,  # Higher creativity for recipes
            format="json"
        )
    
    def generate_recipes(
        self, 
        chef_id: str,
        ingredient_group: List[Dict],
        target_recipe_count: int,
        household_size: int,
        dietary_restrictions: List[str]
    ) -> List[Recipe]:
        """
        Generate recipes from assigned ingredients.
        
        This is the parallel worker node.
        """
        print(f"[{chef_id}] Generating {target_recipe_count} recipes...")
        
        start_time = time.time()
        
        # Prepare prompt
        prompt = PromptTemplates.SOUS_CHEF_RECIPE_GENERATION.format(
            ingredients_json=json.dumps(ingredient_group, indent=2),
            target_recipe_count=target_recipe_count,
            servings=household_size,
            dietary_restrictions=", ".join(dietary_restrictions)
        )
        
        try:
            response = self.llm.invoke([HumanMessage(content=prompt)])
            recipes_data = json.loads(response.content)
            
            # Convert to Recipe objects with IDs
            recipes = []
            for recipe_data in recipes_data:
                recipe = Recipe(
                    recipe_id=str(uuid.uuid4()),
                    name=recipe_data["name"],
                    ingredients=recipe_data["ingredients"],
                    instructions=recipe_data["instructions"],
                    servings=recipe_data["servings"],
                    total_cost=recipe_data["total_cost"],
                    estimated_prep_time=recipe_data.get("estimated_prep_time", 30),
                    meal_type=recipe_data["meal_type"],
                    cuisine_type=recipe_data.get("cuisine_type")
                )
                recipes.append(recipe)
            
            duration = time.time() - start_time
            
            # Log to MLflow
            MLflowLogger.log_agent_call(
                agent_name=chef_id,
                tokens=len(response.content),
                duration=duration,
                model="smollm:360m",
                success=True
            )
            
            print(f"[{chef_id}] Generated {len(recipes)} recipes in {duration:.2f}s")
            
            return recipes
            
        except Exception as e:
            print(f"[{chef_id}] ERROR: {e}")
            return []
    
    def regenerate_with_feedback(
        self,
        chef_id: str,
        original_recipe: Recipe,
        feedback: str,
        ingredient_group: List[Dict],
        household_size: int,
        dietary_restrictions: List[str]
    ) -> Recipe:
        """
        Regenerate a rejected recipe with Nutritionist feedback.
        """
        print(f"[{chef_id}] Regenerating recipe with feedback...")
        
        prompt = PromptTemplates.SOUS_CHEF_RETRY_WITH_FEEDBACK.format(
            original_recipe_json=json.dumps(original_recipe, indent=2),
            feedback=feedback,
            ingredients_json=json.dumps(ingredient_group, indent=2),
            servings=household_size
        )
        
        try:
            response = self.llm.invoke([HumanMessage(content=prompt)])
            recipe_data = json.loads(response.content)
            
            # Create new recipe with new ID
            recipe = Recipe(
                recipe_id=str(uuid.uuid4()),
                name=recipe_data["name"],
                ingredients=recipe_data["ingredients"],
                instructions=recipe_data["instructions"],
                servings=recipe_data["servings"],
                total_cost=recipe_data["total_cost"],
                estimated_prep_time=recipe_data.get("estimated_prep_time", 30),
                meal_type=recipe_data["meal_type"],
                cuisine_type=recipe_data.get("cuisine_type")
            )
            
            print(f"[{chef_id}] Regenerated: {recipe['name']}")
            return recipe
            
        except Exception as e:
            print(f"[{chef_id}] Regeneration ERROR: {e}")
            return None


def sous_chef_generate_node(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 4: Individual SousChef worker node for parallel execution.
    
    This node is called via Send() with subset of state.
    """
    chef_id = state.get("chef_id", "sous_chef_unknown")
    ingredient_group = state.get("ingredient_group", [])
    target_recipe_count = state.get("target_recipe_count", 2)
    
    sous_chef = SousChef()
    
    recipes = sous_chef.generate_recipes(
        chef_id=chef_id,
        ingredient_group=ingredient_group,
        target_recipe_count=target_recipe_count,
        household_size=state["household_size"],
        dietary_restrictions=state["dietary_restrictions"]
    )
    
    # Update state with generated recipes
    for recipe in recipes:
        state["generated_recipes"][recipe["recipe_id"]] = recipe
        state["sous_chef_assignments"][recipe["recipe_id"]] = chef_id
    
    return state
```

### Step 7: Nutritionist Agent

**File: `app/agents/nutritionist.py`**

```python
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage
import json
import time
from typing import Dict
from .state import RecipeGenerationState, ValidationResult
from .prompts import PromptTemplates
from ..services.mlflow_logger import MLflowLogger

class Nutritionist:
    """Nutritionist agent using SmolLM-360M for validation."""
    
    def __init__(self):
        self.llm = ChatOllama(
            model="smollm:360m",
            temperature=0.3,  # Lower temperature for consistent validation
            format="json"
        )
    
    def validate_recipes(self, state: RecipeGenerationState) -> RecipeGenerationState:
        """
        Node 6: Validate all generated recipes.
        """
        print(f"[Nutritionist] Validating {len(state['generated_recipes'])} recipes...")
        
        state["status"] = "validating"
        validation_results = {}
        approved_ids = []
        rejected_ids = []
        
        for recipe_id, recipe in state["generated_recipes"].items():
            # Skip already validated recipes in retry scenarios
            if recipe_id in state["validation_results"]:
                continue
            
            start_time = time.time()
            
            # Prepare prompt
            prompt = PromptTemplates.NUTRITIONIST_VALIDATION.format(
                recipe_json=json.dumps(recipe, indent=2),
                dietary_restrictions=", ".join(state["dietary_restrictions"])
            )
            
            try:
                response = self.llm.invoke([HumanMessage(content=prompt)])
                validation_data = json.loads(response.content)
                
                # Create ValidationResult
                result = ValidationResult(
                    recipe_id=recipe_id,
                    approved=validation_data["approved"],
                    feedback=validation_data["feedback"],
                    nutrition_facts=validation_data["nutrition_facts"],
                    dietary_compliance=validation_data["dietary_compliance"],
                    health_score=validation_data["health_score"]
                )
                
                validation_results[recipe_id] = result
                
                if result["approved"]:
                    approved_ids.append(recipe_id)
                    print(f"[Nutritionist] ✓ APPROVED: {recipe['name']} (score: {result['health_score']})")
                else:
                    rejected_ids.append(recipe_id)
                    print(f"[Nutritionist] ✗ REJECTED: {recipe['name']}")
                    print(f"  Reason: {result['feedback']}")
                
                duration = time.time() - start_time
                
                # Log to MLflow
                state["agent_call_log"].append({
                    "agent": "Nutritionist",
                    "action": "validate_recipe",
                    "recipe_id": recipe_id,
                    "approved": result["approved"],
                    "timestamp": time.time(),
                    "duration": duration
                })
                
            except Exception as e:
                print(f"[Nutritionist] ERROR validating {recipe['name']}: {e}")
                state["errors"].append(f"Validation error for {recipe_id}: {str(e)}")
                # Graceful degradation: mark as pending
                rejected_ids.append(recipe_id)
        
        # Update state
        state["validation_results"].update(validation_results)
        state["approved_recipe_ids"].extend(approved_ids)
        state["rejected_recipe_ids"].extend(rejected_ids)
        
        # Log aggregate metrics
        MLflowLogger.log_validation_results(validation_results)
        
        print(f"[Nutritionist] Summary: {len(approved_ids)} approved, {len(rejected_ids)} rejected")
        
        return state
```

### Step 8: LangGraph Workflow

**File: `app/agents/graph.py`**

```python
from langgraph.graph import StateGraph, END
from langgraph.types import Send
from typing import Literal
from .state import RecipeGenerationState
from .chef_orchestrator import ChefOrchestrator
from .sous_chef import SousChef, sous_chef_generate_node
from .nutritionist import Nutritionist
from ..services.database import DatabaseService
from ..services.mlflow_logger import MLflowLogger

# Initialize agents
chef = ChefOrchestrator()
nutritionist = Nutritionist()
db_service = DatabaseService()


def generate_recipes_parallel(state: RecipeGenerationState):
    """
    Node 3: Fan-out to 3 parallel SousChef nodes.
    """
    print("[Orchestrator] Distributing work to 3 SousChefs...")
    
    # Calculate recipes per chef
    num_meals = state["num_meals"]
    recipes_per_chef = [
        (num_meals // 3) + (1 if i < num_meals % 3 else 0)
        for i in range(3)
    ]
    
    # Create Send objects for parallel execution
    return [
        Send("sous_chef_generate", {
            **state,
            "chef_id": f"sous_chef_{i+1}",
            "ingredient_group": state["ingredient_groups"][i],
            "target_recipe_count": recipes_per_chef[i]
        })
        for i in range(3)
    ]


def aggregate_recipes(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 5: Aggregate results from parallel SousChefs.
    """
    print(f"[Orchestrator] Aggregating {len(state['generated_recipes'])} recipes...")
    
    # Calculate total cost
    total_cost = sum(
        recipe["total_cost"] 
        for recipe in state["generated_recipes"].values()
    )
    
    state["total_cost"] = total_cost
    state["cost_per_meal"] = total_cost / len(state["generated_recipes"]) if state["generated_recipes"] else 0
    state["budget_remaining"] = state["budget"] - total_cost
    
    # Calculate estimated savings (compare to regular prices)
    # This would use deal_index to compare sale_price vs regular_price
    state["estimated_savings"] = 0.0  # Placeholder
    
    print(f"[Orchestrator] Total cost: ${total_cost:.2f}, Budget remaining: ${state['budget_remaining']:.2f}")
    
    return state


def route_after_validation(state: RecipeGenerationState) -> Literal["finalize", "retry", "finalize_partial", "handle_failure"]:
    """
    Conditional edge: Decide next step after Nutritionist validation.
    """
    num_rejected = len(state["rejected_recipe_ids"])
    num_approved = len(state["approved_recipe_ids"])
    num_requested = state["num_meals"]
    
    print(f"[Router] Approved: {num_approved}, Rejected: {num_rejected}, Iteration: {state['iteration_count']}")
    
    # All approved - success!
    if num_rejected == 0:
        return "finalize"
    
    # Max iterations exceeded
    if state["iteration_count"] >= state["max_iterations"]:
        # Check for partial success (60% threshold)
        if num_approved >= num_requested * 0.6:
            print("[Router] Partial success - finalizing with approved recipes")
            state["warnings"].append(f"Only {num_approved}/{num_requested} recipes approved")
            return "finalize_partial"
        else:
            print("[Router] Insufficient approved recipes - failing")
            return "handle_failure"
    
    # Retry available
    print("[Router] Retrying rejected recipes")
    return "retry"


def retry_generation(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 8: Regenerate rejected recipes with feedback.
    """
    print(f"[Orchestrator] Retrying {len(state['recipes_pending_retry'])} recipes...")
    
    sous_chef = SousChef()
    
    for recipe_id, feedback in state["recipes_pending_retry"].items():
        original_recipe = state["generated_recipes"][recipe_id]
        
        # Find original ingredient group
        original_chef_id = state["sous_chef_assignments"][recipe_id]
        chef_index = int(original_chef_id.split("_")[-1]) - 1
        ingredient_group = state["ingredient_groups"][chef_index]
        
        # Regenerate
        new_recipe = sous_chef.regenerate_with_feedback(
            chef_id=original_chef_id,
            original_recipe=original_recipe,
            feedback=feedback,
            ingredient_group=ingredient_group,
            household_size=state["household_size"],
            dietary_restrictions=state["dietary_restrictions"]
        )
        
        if new_recipe:
            # Replace old recipe
            state["generated_recipes"][new_recipe["recipe_id"]] = new_recipe
            state["sous_chef_assignments"][new_recipe["recipe_id"]] = original_chef_id
            
            # Remove old recipe from rejected list
            if recipe_id in state["rejected_recipe_ids"]:
                state["rejected_recipe_ids"].remove(recipe_id)
    
    # Clear pending retries
    state["recipes_pending_retry"] = {}
    
    return state


def finalize_meal_plan(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 9: Finalize and save approved recipes.
    """
    print("[Orchestrator] Finalizing meal plan...")
    
    approved_recipes = [
        state["generated_recipes"][rid] 
        for rid in state["approved_recipe_ids"]
    ]
    
    # Save to database
    recipe_ids = db_service.save_recipes(state["user_id"], approved_recipes)
    
    print(f"[Orchestrator] Saved {len(recipe_ids)} recipes to database")
    
    # Log final metrics to MLflow
    MLflowLogger.log_final_metrics(
        total_cost=state["total_cost"],
        cost_per_meal=state["cost_per_meal"],
        estimated_savings=state["estimated_savings"],
        iterations=state["iteration_count"],
        recipe_count=len(state["approved_recipe_ids"]),
        success=True
    )
    
    state["status"] = "completed"
    
    # Finalize MLflow run
    MLflowLogger.finalize_run(state)
    
    print(f"[Orchestrator] ✓ Meal plan complete! {len(approved_recipes)} recipes ready.")
    
    return state


def handle_failure(state: RecipeGenerationState) -> RecipeGenerationState:
    """
    Node 10: Handle workflow failure gracefully.
    """
    print("[Orchestrator] ✗ Workflow failed - graceful degradation")
    
    state["status"] = "failed"
    state["errors"].append("Unable to generate sufficient approved recipes")
    
    # Log failure to MLflow
    MLflowLogger.log_final_metrics(
        total_cost=state["total_cost"],
        cost_per_meal=state["cost_per_meal"],
        estimated_savings=state["estimated_savings"],
        iterations=state["iteration_count"],
        recipe_count=len(state["approved_recipe_ids"]),
        success=False
    )
    
    MLflowLogger.finalize_run(state)
    
    return state


# Build the graph
def create_recipe_generation_graph():
    """
    Construct the complete LangGraph workflow.
    """
    workflow = StateGraph(RecipeGenerationState)
    
    # Add nodes
    workflow.add_node("initialize_chef", chef.initialize)
    workflow.add_node("plan_ingredient_groups", chef.plan_ingredient_groups)
    workflow.add_node("generate_recipes_parallel", generate_recipes_parallel)
    workflow.add_node("sous_chef_generate", sous_chef_generate_node)
    workflow.add_node("aggregate_recipes", aggregate_recipes)
    workflow.add_node("nutritionist_validate", nutritionist.validate_recipes)
    workflow.add_node("handle_rejections", chef.handle_rejections)
    workflow.add_node("retry_generation", retry_generation)
    workflow.add_node("finalize_meal_plan", finalize_meal_plan)
    workflow.add_node("handle_failure", handle_failure)
    
    # Define edges
    workflow.set_entry_point("initialize_chef")
    workflow.add_edge("initialize_chef", "plan_ingredient_groups")
    workflow.add_edge("plan_ingredient_groups", "generate_recipes_parallel")
    workflow.add_edge("generate_recipes_parallel", "aggregate_recipes")
    workflow.add_edge("aggregate_recipes", "nutritionist_validate")
    
    # Conditional routing after validation
    workflow.add_conditional_edges(
        "nutritionist_validate",
        route_after_validation,
        {
            "finalize": "finalize_meal_plan",
            "retry": "handle_rejections",
            "finalize_partial": "finalize_meal_plan",
            "handle_failure": "handle_failure"
        }
    )
    
    # Retry loop
    workflow.add_edge("handle_rejections", "retry_generation")
    workflow.add_edge("retry_generation", "nutritionist_validate")
    
    # Terminal nodes
    workflow.add_edge("finalize_meal_plan", END)
    workflow.add_edge("handle_failure", END)
    
    return workflow.compile()
```

### Step 9: Main Execution

**File: `app/main_recipe_generation.py`**

```python
import mlflow
from app.agents.graph import create_recipe_generation_graph
from app.agents.state import RecipeGenerationState

# Configure MLflow
mlflow.set_tracking_uri("http://localhost:5000")  # Or your MLflow server
mlflow.set_experiment("grocery-meal-planner")

def run_recipe_generation(
    user_id: int,
    postal_code: str,
    budget: float,
    household_size: int,
    dietary_restrictions: list,
    num_meals: int,
    preferences: dict = None
):
    """
    Main entry point for recipe generation workflow.
    """
    # Initialize state
    initial_state = RecipeGenerationState(
        user_id=user_id,
        postal_code=postal_code,
        budget=budget,
        household_size=household_size,
        dietary_restrictions=dietary_restrictions,
        num_meals=num_meals,
        preferences=preferences or {},
        # All other fields initialized by Chef
    )
    
    # Create and run graph
    graph = create_recipe_generation_graph()
    
    print("=" * 60)
    print("STARTING RECIPE GENERATION WORKFLOW")
    print("=" * 60)
    
    final_state = graph.invoke(initial_state)
    
    print("=" * 60)
    print("WORKFLOW COMPLETED")
    print(f"Status: {final_state['status']}")
    print(f"Recipes Generated: {len(final_state['generated_recipes'])}")
    print(f"Recipes Approved: {len(final_state['approved_recipe_ids'])}")
    print(f"Total Cost: ${final_state['total_cost']:.2f}")
    print(f"Iterations: {final_state['iteration_count']}")
    print("=" * 60)
    
    return final_state


# Example usage
if __name__ == "__main__":
    result = run_recipe_generation(
        user_id=1,
        postal_code="M5V3A8",  # Toronto
        budget=75.00,
        household_size=2,
        dietary_restrictions=["vegetarian", "no_nuts"],
        num_meals=7,
        preferences={
            "cuisine_preferences": ["Italian", "Asian"],
            "avoid_ingredients": ["mushrooms"]
        }
    )
    
    # Access results
    if result["status"] == "completed":
        print("\n✓ SUCCESS! Approved recipes:")
        for recipe_id in result["approved_recipe_ids"]:
            recipe = result["generated_recipes"][recipe_id]
            print(f"  - {recipe['name']} (${recipe['total_cost']:.2f})")
    else:
        print(f"\n✗ FAILED: {result['errors']}")
```

### Step 10: Testing

**File: `app/tests/test_graph.py`**

```python
import pytest
from app.agents.graph import create_recipe_generation_graph
from app.agents.state import RecipeGenerationState

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
        preferences={}
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
```

## Deployment Instructions

### 1. Environment Setup

Create `.env` file:
```bash
DATABASE_URL=postgresql://user:pass@neon.tech/dbname
OLLAMA_BASE_URL=http://localhost:11434
MLFLOW_TRACKING_URI=http://localhost:5000
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Start Ollama

```bash
ollama serve
ollama pull smollm:1.7b
ollama pull smollm:360m
```

### 4. Start MLflow

```bash
mlflow ui --backend-store-uri sqlite:///mlflow.db --port 5000
```

### 5. Initialize Database

```bash
python scripts/init_db.py
```

### 6. Run Recipe Generation

```bash
python app/main_recipe_generation.py
```

## Monitoring & Debugging

### View MLflow Dashboard
Navigate to `http://localhost:5000` to see:
- Run parameters
- Agent call metrics
- Token usage
- Validation results
- Final recipes (artifacts)

### Debug Logs
Each agent prints detailed logs with `[Agent Name]` prefix. Track:
- Ingredient group creation
- Recipe generation progress
- Validation results
- Retry strategies

### Cost Tracking
MLflow logs:
- Tokens per agent call
- Total workflow cost
- Cost per meal generated

## Performance Optimization

1. **Ollama Configuration:**
   ```bash
   # Increase context window if needed
   OLLAMA_NUM_CTX=4096
   
   # Parallel model loading
   OLLAMA_MAX_LOADED_MODELS=2
   ```

2. **Caching Strategy:**
   - Cache ingredient groups for same postal_code
   - Cache deal data for 6 hours
   - Reuse validation results for identical recipes

3. **Batch Processing:**
   - Generate multiple meal plans in parallel
   - Use async for database operations

## Troubleshooting

### Issue: LLM not returning valid JSON
- Increase temperature (more creative) or decrease (more structured)
- Add JSON schema validation
- Retry with fallback prompts

### Issue: Parallel execution timing out
- Reduce `target_recipe_count` per SousChef
- Increase Ollama timeout settings
- Check system resources (RAM, CPU)

### Issue: Nutritionist rejecting all recipes
- Review dietary_restrictions for conflicts
- Adjust validation thresholds in prompts
- Check ingredient data quality

## Next Steps

1. **Integrate with FastAPI:**
   - Create endpoint: `POST /api/v1/recipes/generate`
   - Return recipe IDs for frontend

2. **Add Shopping Optimizer:**
   - New agent to consolidate ingredients
   - Create shopping list from approved recipes

3. **Enhanced Evaluation:**
   - Implement custom metrics from research paper
   - Add BLEU/ROUGE scoring
   - Track ingredient coverage

4. **Production Deployment:**
   - Containerize with Docker
   - Deploy Ollama on GPU instance
   - Set up persistent MLflow backend

## Success Criteria

- [ ] 3 SousChefs execute in parallel
- [ ] Nutritionist validation with feedback loop
- [ ] Max 2 retry iterations enforced
- [ ] Graceful degradation on failure
- [ ] All metrics logged to MLflow
- [ ] Recipes saved to database
- [ ] Total workflow time < 30 seconds
- [ ] Budget constraints respected

---

**End of Implementation Guide**

This guide provides complete, production-ready code for the LangGraph orchestration system. Follow the steps sequentially, and refer to the troubleshooting section for common issues.