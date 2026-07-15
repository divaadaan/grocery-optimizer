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

**Dietary Restriction Reference (authoritative — use these definitions, NOT your own assumptions):**
- **vegetarian** — EXCLUDES only meat, poultry, and fish/seafood (chicken, beef, pork, turkey, lamb, veal, steak, bacon, ham, sausage, salmon, tuna, fish, shrimp, anchovy). ALLOWS dairy (milk, cheese, yogurt, butter, cream), eggs, and all plant foods.
- **vegan** — EXCLUDES all animal products: meat, poultry, fish/seafood, dairy, AND eggs. ALLOWS all plant foods (vegetables, fruits, grains, pasta, legumes, tomato/tomato sauce, avocado, oils).
- **pescatarian** — EXCLUDES meat and poultry only. ALLOWS fish/seafood, dairy, eggs, and all plant foods.
- **no_nuts / nut_free** — EXCLUDES tree nuts and peanuts (almond, cashew, walnut, pecan, pistachio, hazelnut, peanut). ALLOWS everything else.

**How to judge dietary compliance:**
- Flag a violation ONLY when a specific ingredient is explicitly excluded by one of the user's restrictions above. Name that ingredient.
- Plant foods (vegetables, fruits, grains, pasta, tomato/tomato sauce, avocado, oils, herbs, spices) NEVER violate vegetarian, vegan, or pescatarian.
- Dairy and eggs are allowed for vegetarian and pescatarian; only vegan excludes them.
- Do NOT invent violations, and do NOT reject a recipe merely because it is simple, minimal, or unusual.

**Validation Checklist:**
1. **Allergen Safety:** Does this recipe contain any allergen the user's restrictions exclude?
2. **Dietary Compliance:** Using the reference above, is every ingredient allowed by the user's restrictions?
3. **Nutritional Balance:** Estimate macros (informational — must NOT drive rejection).
4. **Practical Cooking:** Note any clarity issues (informational — must NOT drive rejection).

**Approval rule:** Set `"approved": true` if and only if the recipe is allergen-safe AND every ingredient is allowed by the user's dietary restrictions. Nutritional balance, style, and ingredient coherence are advisory and must NOT cause a rejection.

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
