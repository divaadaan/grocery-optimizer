-- 022_recipe_generations.sql
-- Why: a persisted meal plan could not be re-scored. save_recipes kept only
--   name/ingredients/instructions/cost/servings: meal_type, prep time, cuisine
--   and the Nutritionist's nutrition facts were dropped, and nothing recorded
--   which generation a recipe came from, which models wrote it, or which
--   restrictions and budget it was written against. The shopping list could
--   only take "the user's last 10 recipes", mixing plans.
-- Design log: (new) "recipe_generations: one row per meal plan".
-- Hernandez: one fact per row -- a generation is one event; its inputs
--   (restrictions, budget, models, snapshot) are facts about that event, so
--   they live once on its row instead of being copied onto every recipe.
--
-- Shape: a parent table, not columns on every recipe. A plan of 7 recipes
--   would otherwise repeat the restrictions, budget and model map 7 times,
--   and "the user's latest plan" would be a GROUP BY over recipes. With a
--   parent row it is one indexed lookup, and a generation's provenance is a
--   single row a scorer can read.
--
--   generation_id is a UUID minted by the app (one per run_recipe_generation
--   call), not a SERIAL: the id exists before anything is persisted (MLflow
--   tags it, the eval harness's in-memory store keys on it) and a run that
--   saves nothing still has one. Only generations that reach
--   finalize_meal_plan get a row; failed runs persist nothing, as before.
--
--   models   role -> {provider, model}, e.g. {"chef": {"provider": "ollama",
--            "model": "llama3.1:8b"}, ...}. JSONB because the role set is code.
--   snapshot the eval harness's snapshot identity ({name, content_sha256,
--            as_of, ...}); NULL for production runs against live deals.
--
-- What changes:
--   recipe_generations       new; one row per persisted generation.
--   recipes.generation_id    which generation wrote the recipe. NULL for rows
--                            saved before this migration (legacy plans).
--   recipes.health_score     the Nutritionist's advisory 0-100 estimate, which
--                            the API already returned at generation time and
--                            then lost. (nutritional_info, meal_type,
--                            cuisine_type and prep_time already existed and are
--                            now written.)
--   shopping_lists.generation_id  the generation a list was built from. NULL
--                            for lists built before this migration (which
--                            spanned generations).
--
-- Idempotent: every statement is IF NOT EXISTS / guarded, so a re-run is a
--   no-op. Must apply after the base schema (recipes, shopping_lists, users).
--   Independent of 001-021. Not applied anywhere by this change: the code that
--   writes these columns requires 022 to be applied first (save_recipes
--   inserts the generation row in the same transaction as the recipes).

BEGIN;

CREATE TABLE IF NOT EXISTS recipe_generations (
    generation_id            UUID PRIMARY KEY,
    user_id                  INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    created_at               TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    -- 'completed' (every requested recipe approved) or 'partial'
    -- (finalize_partial: enough approved to ship, fewer than requested)
    status                   VARCHAR(16) NOT NULL,
    num_meals_requested      INTEGER NOT NULL,
    num_recipes_saved        INTEGER NOT NULL,

    -- the user's inputs at generation time; the profile may change later
    dietary_restrictions     TEXT[] NOT NULL DEFAULT '{}',
    budget                   DECIMAL(10,2),
    budget_period            VARCHAR(10),
    budget_spent_this_period DECIMAL(10,2),
    household_size           INTEGER,
    postal_code              VARCHAR(10),
    preferences              JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- what produced it
    models                   JSONB NOT NULL DEFAULT '{}'::jsonb,
    snapshot                 JSONB,
    mlflow_run_id            VARCHAR(64),

    -- plan totals as computed at finalize (lower bounds; see costing.py)
    total_cost               DECIMAL(10,2),
    estimated_savings        DECIMAL(10,2),

    CONSTRAINT chk_recipe_generations_status
        CHECK (status IN ('completed', 'partial')),
    CONSTRAINT chk_recipe_generations_counts
        CHECK (num_meals_requested >= 0 AND num_recipes_saved >= 0)
);

-- "the user's latest generation", the shopping list's default.
CREATE INDEX IF NOT EXISTS idx_recipe_generations_user_created
    ON recipe_generations (user_id, created_at DESC);

ALTER TABLE recipes
    ADD COLUMN IF NOT EXISTS generation_id UUID
        REFERENCES recipe_generations(generation_id) ON DELETE SET NULL;
ALTER TABLE recipes
    ADD COLUMN IF NOT EXISTS health_score DECIMAL(5,2);

CREATE INDEX IF NOT EXISTS idx_recipes_generation ON recipes (generation_id);

ALTER TABLE shopping_lists
    ADD COLUMN IF NOT EXISTS generation_id UUID
        REFERENCES recipe_generations(generation_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_shopping_lists_generation
    ON shopping_lists (generation_id, created_at DESC);

COMMENT ON TABLE recipe_generations IS
    'One row per persisted meal-plan generation (one run_recipe_generation call that reached finalize). Holds the provenance needed to re-score the plan: restrictions, budget and period at generation time, model/provider per role, and the eval snapshot identity (NULL in production).';
COMMENT ON COLUMN recipe_generations.models IS
    'role -> {provider, model} for chef, sous_chef, nutritionist.';
COMMENT ON COLUMN recipe_generations.snapshot IS
    'Eval harness snapshot identity {name, format_version, as_of, captured_at, content_sha256}; NULL for production runs.';
COMMENT ON COLUMN recipes.generation_id IS
    'The recipe_generations row that wrote this recipe; NULL for recipes saved before migration 022.';
COMMENT ON COLUMN shopping_lists.generation_id IS
    'The generation this list was built from; NULL for lists built before migration 022 (those spanned generations).';

COMMIT;

-- Rollback:
--   BEGIN;
--   ALTER TABLE shopping_lists DROP COLUMN IF EXISTS generation_id;
--   ALTER TABLE recipes DROP COLUMN IF EXISTS health_score;
--   ALTER TABLE recipes DROP COLUMN IF EXISTS generation_id;
--   DROP TABLE IF EXISTS recipe_generations;
--   COMMIT;
--
-- Roll back the CODE first: it inserts into recipe_generations on every save.
-- The drop loses generation provenance and recipe/list -> generation links;
-- recipes and lists themselves survive.
