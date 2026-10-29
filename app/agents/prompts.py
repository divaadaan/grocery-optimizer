class PromptTemplates:
    """Centralized prompt templates for all agents."""

    CHEF_INGREDIENT_PLANNING = """You are a professional Chef Orchestrator \
managing a meal planning system.

Your task: Analyze available grocery deals and create 3 optimized ingredient \
groups for your team of 3 SousChefs.

**Available Deals:**
{deals_json}

**User Requirements:**
- Budget: ${budget}
- Household Size: {household_size} people
- Number of Meals: {num_meals}
- Dietary Restrictions: {dietary_restrictions}
- Preferences: {preferences}

**Optimization Strategy:**
1. Maximize ingredient reuse across groups (prefer ingredients that appear in \
multiple groups)
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

    SOUS_CHEF_RECIPE_GENERATION = """You are a creative SousChef specializing in \
practical, delicious recipes.

**Your Assigned Deals (cite each by its deal_id):**
{ingredients_json}

**Pantry Staples Already On Hand (cite each by its product_id):**
{pantry_json}

**Recipe Requirements:**
- Number of recipes to create: {target_recipe_count}
- Servings per recipe: {servings}
- Dietary restrictions: {dietary_restrictions}
- Use ONLY the assigned deals and the pantry staples listed above. Nothing else.
- EVERY ingredient must cite exactly one source: "deal_id" for an assigned deal,
  OR "product_id" for a pantry staple. Copy the id exactly from the lists above.
- If an ingredient is not in either list, leave it out of the recipe.
- "name" is how the ingredient reads in the recipe; costs are computed for you.

**Instruction Format:**
- Write EVERY ingredient in "instructions" as a placeholder {{N}}, where N is
  that ingredient's 0-based position in this recipe's "ingredients" list
  ({{0}} is the first ingredient, {{1}} the second, ...).
- Never type an ingredient or food name in a step; use its placeholder. The
  placeholder is replaced with the ingredient's name for you.
- Use no other curly braces in the steps.
- Every ingredient should appear in at least one step.

**Output Format (JSON array):**
[
  {{
    "name": "Recipe Name",
    "meal_type": "breakfast|lunch|dinner",
    "cuisine_type": "Italian|Mexican|Asian|American|etc",
    "ingredients": [
      {{"name": "...", "deal_id": 123, "quantity": "...", "unit": "..."}},
      {{"name": "...", "product_id": 45, "quantity": "...", "unit": "..."}}
    ],
    "instructions": ["Heat {{1}} in a pan.", "Add {{0}} and cook for 5 minutes.", ...],
    "estimated_prep_time": 30,
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

**Your Assigned Deals (cite each by its deal_id):**
{ingredients_json}

**Pantry Staples Already On Hand (cite each by its product_id):**
{pantry_json}

**Task:**
Create a NEW recipe using only these deals and staples that addresses the feedback.
- Fix the specific issues mentioned: drop any ingredient the feedback names
- Maintain dietary compliance
- Ensure nutritional balance
- EVERY ingredient must cite exactly one source: "deal_id" for an assigned deal,
  OR "product_id" for a pantry staple, copied exactly from the lists above

**Instruction Format:**
- Write EVERY ingredient in "instructions" as a placeholder {{N}}, where N is
  that ingredient's 0-based position in your NEW recipe's "ingredients" list
  ({{0}} is the first ingredient, {{1}} the second, ...).
- Never type an ingredient or food name in a step; use its placeholder. The
  placeholder is replaced with the ingredient's name for you.
- Use no other curly braces in the steps.
- Every ingredient should appear in at least one step.

**Output Format (JSON):**
{{
  "name": "Revised Recipe Name",
  "meal_type": "...",
  "ingredients": [
    {{"name": "...", "deal_id": 123, "quantity": "...", "unit": "..."}},
    {{"name": "...", "product_id": 45, "quantity": "...", "unit": "..."}}
  ],
  "instructions": ["Heat {{1}} in a pan.", "Add {{0}} and cook for 5 minutes.", ...],
  "servings": {servings}
}}

Generate the revised recipe now:"""

    NUTRITIONIST_VALIDATION = """You are a professional Nutritionist validating \
recipe quality and safety.

**Recipe to Validate:**
{recipe_json}

**User's Dietary Restrictions:**
{dietary_restrictions}

**Not your call:** dietary compliance and allergen safety are decided in code \
from the curated attributes of the deals and pantry products each ingredient \
cites. Your `approved` and `dietary_compliance` fields are recorded but never \
decide anything, so do not try to judge restrictions.

**Review Checklist:**
1. **Nutritional Balance:** Are macros balanced? (protein, carbs, healthy fats)
2. **Practical Cooking:** Are instructions clear and achievable?
3. **Ingredient Coherence:** Do ingredients work well together?

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

    CHEF_NEW_INGREDIENTS_SELECTION = """You are a Chef Orchestrator selecting \
alternative ingredients.

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
