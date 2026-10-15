-- 012_budget_period_and_spend_time.sql
-- Why: `users.budget` was documented as a *monthly* budget but applied as the
--   ceiling for a *single* generation (app/agents/graph.py:69), with nothing
--   accumulating against what the user actually spent. Four weekly plans in a
--   month each got the whole month's money, so "within the spend limit you
--   set" was not true of the system. Two columns are needed to make it true,
--   and they are one concern: a budget period is only a real thing if you can
--   say how long it is AND when money landed inside it. Neither column does
--   anything on its own.
--
--   `users.budget_period` ('weekly' | 'monthly') makes the period data instead
--   of prose. It was prose in three places that disagreed: the register
--   docstring said monthly, the code behaved per-generation, and the frontend
--   label said "Weekly budget (CAD)". Default 'monthly' preserves the
--   documented meaning for the 5 existing rows.
--
--   `shopping_lists.completed_at` dates the spend. `is_completed` says *that*
--   a list was bought but not *when*, and `created_at` is when it was
--   generated -- a list built 31 Jan and bought 2 Feb is February's money.
--   Without a completion timestamp, spend cannot be attributed to a period at
--   all. Backfilled from `created_at` for rows already complete: an
--   approximation, and the only one available, flagged here rather than
--   silently presented as a purchase time.
-- Design log: (new) "the budget period becomes a defined thing".
-- Hernandez: an attribute belongs to the entity it describes, and a fact the
--   application needs (when was this bought) is a column, not an inference
--   from a different column. Also: no stored running total -- spend is derived
--   from these rows on read, so there is nothing to drift.

BEGIN;

-- --------------------------------------------------------------------------
-- users: how long is a budget period
-- --------------------------------------------------------------------------
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS budget_period VARCHAR(16) NOT NULL DEFAULT 'monthly';

-- Enumerated in the constraint rather than a lookup table: two values, no
-- attributes of their own, and app/services/budget.py must know each one's
-- calendar arithmetic by name regardless.
ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_budget_period_check;
ALTER TABLE users
    ADD CONSTRAINT users_budget_period_check
    CHECK (budget_period IN ('weekly', 'monthly'));

COMMENT ON COLUMN users.budget IS
    'Grocery spend limit for ONE budget period, in CAD. The period is budget_period -- this is not a per-meal-plan or per-generation ceiling. Remaining budget is budget minus the total_cost of shopping_lists completed inside the current period (app/services/budget.py).';
COMMENT ON COLUMN users.budget_period IS
    'Length of the window users.budget applies to: ''weekly'' (Monday-anchored ISO week) or ''monthly'' (calendar month from the 1st). Calendar-anchored, not rolling, so the window resets predictably and never re-dates an old purchase.';

-- --------------------------------------------------------------------------
-- shopping_lists: when the money was spent
-- --------------------------------------------------------------------------
ALTER TABLE shopping_lists
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;

-- Backfill before the CHECK below, so an already-completed list cannot fail it.
UPDATE shopping_lists
SET completed_at = created_at
WHERE is_completed IS TRUE
  AND completed_at IS NULL;

-- is_completed and completed_at are two views of one fact, so make them
-- unable to disagree. (A generated column would be stricter still, but
-- Postgres cannot convert an existing column into a generated one, and
-- dropping/re-adding is_completed would break every reader for the sake of
-- something this constraint already guarantees.)
ALTER TABLE shopping_lists
    DROP CONSTRAINT IF EXISTS shopping_lists_completion_consistent;
ALTER TABLE shopping_lists
    ADD CONSTRAINT shopping_lists_completion_consistent
    CHECK (is_completed = (completed_at IS NOT NULL));

COMMENT ON COLUMN shopping_lists.completed_at IS
    'When the user marked this list bought (POST /shopping-list/{user_id}/mark-complete). The spend date for budget accumulation -- created_at is generation time and must not be used for it. NULL means not bought, which is not spend. Rows predating migration 012 carry created_at as an approximation.';

-- The budget query is "this user's completed lists since <period start>".
CREATE INDEX IF NOT EXISTS idx_shopping_lists_user_completed_at
    ON shopping_lists (user_id, completed_at)
    WHERE completed_at IS NOT NULL;

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP INDEX IF EXISTS idx_shopping_lists_user_completed_at;
--   ALTER TABLE shopping_lists
--       DROP CONSTRAINT IF EXISTS shopping_lists_completion_consistent;
--   ALTER TABLE shopping_lists DROP COLUMN IF EXISTS completed_at;
--   ALTER TABLE users DROP CONSTRAINT IF EXISTS users_budget_period_check;
--   ALTER TABLE users DROP COLUMN IF EXISTS budget_period;
--   COMMIT;
--
-- Structurally reversible; lossy in data. Dropping completed_at discards every
-- real purchase timestamp recorded after this migration -- is_completed
-- survives, so "bought" is recoverable but "when" is not, and budget periods
-- become uncomputable again. Dropping budget_period loses any non-default
-- choice a user made.
