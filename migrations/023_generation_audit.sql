-- 023_generation_audit.sql
-- Why: a recipe-generation run could not be audited from the database. A
--   failed run persisted nothing, a saved recipe kept only its rendered
--   instruction text (so it could not be re-scored structurally), and the
--   Nutritionist's compliance verdict sat in allergen_info, a column named
--   for something else.
-- Design log: "recipe_generations: one row per meal plan" (extends 022);
--   ROADMAP "Next up (2026-10-29)" items 1, 3 and 4.
-- Hernandez: one fact per row, one column per fact -- a failed run is still
--   one generation event; a compliance verdict is not allergen information.
--
-- What changes:
--
--   recipe_generations.status         now also 'failed' (handle_failure). A
--                                     failed row carries the same provenance
--                                     a finalized one does, saves 0 recipes,
--                                     and has NULL total_cost /
--                                     estimated_savings (there is no plan).
--   recipe_generations.failure_reason why the run failed, human-readable
--                                     ("Planning failed: ..." or
--                                     "Insufficient approved recipes: ...").
--                                     Set iff status = 'failed'.
--   recipe_generations.iteration_count       retry iterations used.
--   recipe_generations.num_recipes_generated recipes the SousChefs produced
--                                            over the whole run (including
--                                            ones later regenerated).
--   recipe_generations.num_recipes_approved  approved at the end of the run;
--                                            can exceed num_recipes_saved
--                                            (surplus trimmed) or fall short
--                                            of num_meals_requested (failed /
--                                            partial).
--   recipe_generations.num_recipes_rejected  still rejected at the end.
--     The four counts are written for every new row, finalized or failed:
--     with num_meals_requested they say whether a run fell short on
--     compliance (rejected), on SousChef output (generated), or ran out of
--     retries (iteration_count). NULL for rows saved before this migration.
--
--   recipes.instruction_steps   the SousChef's placeholder templates and their
--                               ingredient refs ([{template, ingredient_refs}],
--                               agents/instruction_refs.py), next to the
--                               rendered `instructions` text, so a saved recipe
--                               can be re-scored structurally
--                               (eval/scorers.py). NULL for recipes saved
--                               before this migration.
--   recipes.dietary_compliance  the Nutritionist's verdict
--                               ({allergen_free, meets_restrictions,
--                               violations}). The ONE source of truth for it
--                               from here on: the app writes it here and reads
--                               only this column.
--
-- Backfill: rows saved under 022 hold the verdict in allergen_info (nothing
--   before 022 wrote allergen_info). Those are copied into
--   dietary_compliance where it is still NULL and allergen_info has the
--   verdict's shape (an object with a meets_restrictions key). allergen_info
--   is left as it was (additive first -- migrations/README.md rule 3) but is
--   no longer written or read; dropping or clearing it is a later migration.
--   If the two ever disagree, dietary_compliance wins.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS; each CHECK is dropped IF EXISTS and
--   re-added; the backfill only touches rows whose dietary_compliance is
--   still NULL. A re-run is a no-op. Requires 022.
--
-- Deploy order: apply 023 BEFORE deploying the code that writes these columns
--   (save_recipes inserts failure_reason/counts and the two recipe columns).

BEGIN;

-- ---------------------------------------------------------------- generations

ALTER TABLE recipe_generations
    ADD COLUMN IF NOT EXISTS failure_reason TEXT;
ALTER TABLE recipe_generations
    ADD COLUMN IF NOT EXISTS iteration_count INTEGER;
ALTER TABLE recipe_generations
    ADD COLUMN IF NOT EXISTS num_recipes_generated INTEGER;
ALTER TABLE recipe_generations
    ADD COLUMN IF NOT EXISTS num_recipes_approved INTEGER;
ALTER TABLE recipe_generations
    ADD COLUMN IF NOT EXISTS num_recipes_rejected INTEGER;

ALTER TABLE recipe_generations
    DROP CONSTRAINT IF EXISTS chk_recipe_generations_status;
ALTER TABLE recipe_generations
    ADD CONSTRAINT chk_recipe_generations_status
        CHECK (status IN ('completed', 'partial', 'failed'));

-- A reason exactly when the run failed, and a failed run saved nothing.
ALTER TABLE recipe_generations
    DROP CONSTRAINT IF EXISTS chk_recipe_generations_failure;
ALTER TABLE recipe_generations
    ADD CONSTRAINT chk_recipe_generations_failure
        CHECK ((status = 'failed') = (failure_reason IS NOT NULL)
               AND (status <> 'failed' OR num_recipes_saved = 0));

ALTER TABLE recipe_generations
    DROP CONSTRAINT IF EXISTS chk_recipe_generations_run_counts;
ALTER TABLE recipe_generations
    ADD CONSTRAINT chk_recipe_generations_run_counts
        CHECK (COALESCE(iteration_count, 0) >= 0
               AND COALESCE(num_recipes_generated, 0) >= 0
               AND COALESCE(num_recipes_approved, 0) >= 0
               AND COALESCE(num_recipes_rejected, 0) >= 0);

-- "the user's failed runs", for debugging; a partial index, failures are rare.
CREATE INDEX IF NOT EXISTS idx_recipe_generations_failed
    ON recipe_generations (user_id, created_at DESC)
    WHERE status = 'failed';

-- -------------------------------------------------------------------- recipes

ALTER TABLE recipes
    ADD COLUMN IF NOT EXISTS instruction_steps JSONB;
ALTER TABLE recipes
    ADD COLUMN IF NOT EXISTS dietary_compliance JSONB;

UPDATE recipes
   SET dietary_compliance = allergen_info
 WHERE dietary_compliance IS NULL
   AND jsonb_typeof(allergen_info) = 'object'
   AND allergen_info ? 'meets_restrictions';

-- ------------------------------------------------------------------- comments

COMMENT ON TABLE recipe_generations IS
    'One row per meal-plan generation (one run_recipe_generation call) that reached finalize_meal_plan or handle_failure. Holds the provenance needed to re-score or debug the run: restrictions, budget and period at generation time, model/provider per role, the eval snapshot identity (NULL in production), and since 023 the failure reason and run counts.';
COMMENT ON COLUMN recipe_generations.failure_reason IS
    'Why the run failed; set iff status = ''failed'' (chk_recipe_generations_failure). NULL for completed/partial runs.';
COMMENT ON COLUMN recipe_generations.iteration_count IS
    'Retry iterations the run used (0 = no retry). NULL for rows saved before migration 023.';
COMMENT ON COLUMN recipe_generations.num_recipes_generated IS
    'Recipes the SousChefs produced over the run, including ones later regenerated. NULL before 023.';
COMMENT ON COLUMN recipe_generations.num_recipes_approved IS
    'Recipes approved at the end of the run; may exceed num_recipes_saved (surplus trimmed). NULL before 023.';
COMMENT ON COLUMN recipe_generations.num_recipes_rejected IS
    'Recipes still rejected at the end of the run. NULL before 023.';
COMMENT ON COLUMN recipes.instruction_steps IS
    'SousChef placeholder steps [{template, ingredient_refs}] behind the rendered instructions; NULL for recipes saved before migration 023.';
COMMENT ON COLUMN recipes.dietary_compliance IS
    'The Nutritionist''s deterministic compliance verdict {allergen_free, meets_restrictions, violations}. Sole source of truth since 023 (backfilled from allergen_info, which 022-era code used for it).';
COMMENT ON COLUMN recipes.allergen_info IS
    'LEGACY: held the compliance verdict for recipes saved under 022; superseded by dietary_compliance (023) and no longer written or read.';

COMMIT;

-- Rollback:
--   BEGIN;
--   DELETE FROM recipe_generations WHERE status = 'failed';
--   ALTER TABLE recipe_generations DROP CONSTRAINT IF EXISTS chk_recipe_generations_failure;
--   ALTER TABLE recipe_generations DROP CONSTRAINT IF EXISTS chk_recipe_generations_run_counts;
--   ALTER TABLE recipe_generations DROP CONSTRAINT IF EXISTS chk_recipe_generations_status;
--   ALTER TABLE recipe_generations ADD CONSTRAINT chk_recipe_generations_status
--       CHECK (status IN ('completed', 'partial'));
--   DROP INDEX IF EXISTS idx_recipe_generations_failed;
--   ALTER TABLE recipe_generations DROP COLUMN IF EXISTS num_recipes_rejected;
--   ALTER TABLE recipe_generations DROP COLUMN IF EXISTS num_recipes_approved;
--   ALTER TABLE recipe_generations DROP COLUMN IF EXISTS num_recipes_generated;
--   ALTER TABLE recipe_generations DROP COLUMN IF EXISTS iteration_count;
--   ALTER TABLE recipe_generations DROP COLUMN IF EXISTS failure_reason;
--   ALTER TABLE recipes DROP COLUMN IF EXISTS instruction_steps;
--   ALTER TABLE recipes DROP COLUMN IF EXISTS dietary_compliance;
--   COMMIT;
--
-- Roll back the CODE first: it writes every column above. Not reversible
-- without data loss: failed-run rows are deleted (the old CHECK cannot hold
-- them), and instruction_steps plus any verdict written after 023 are lost.
-- Verdicts backfilled from allergen_info survive there, because 023 leaves
-- allergen_info untouched; verdicts saved after 023 exist only in
-- dietary_compliance.
