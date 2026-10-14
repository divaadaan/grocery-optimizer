-- 002_deals_source_provenance.sql
-- Why: re-running ingestion for the same postal code and the same flyer week must
--   not duplicate deals, and a user with dietary restrictions must not be served
--   machine-ingested product names (see app/services/deal_gate.py). Both need
--   provenance on the row: a source tag to filter on, and the provider's stable
--   item id to form a natural key the upsert can conflict against.
-- Design log: (new) "Ingested deals need a natural key; flyer_item_id is it".
-- Hernandez: table-level integrity - a table must have a candidate key that is
--   not the surrogate. deals.deal_id is a SERIAL and nothing else was unique, so
--   the table had no way to recognise a row it had already seen.

BEGIN;

ALTER TABLE deals
    ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'seed';

ALTER TABLE deals
    ADD COLUMN IF NOT EXISTS source_item_id VARCHAR(64);

COMMENT ON COLUMN deals.source IS
    'Provenance: seed | manual | flipp. app/services/deal_gate.py withholds non-curated sources from users with dietary restrictions.';
COMMENT ON COLUMN deals.source_item_id IS
    'Provider-side item id (Flipp flyer_item_id); NULL for hand-written rows.';

-- Natural key: this item, from this source, at this store, for this flyer week.
--
-- valid_from is in the key on purpose. Without it, next week's flyer would
-- UPDATE this week's row and the price history would be overwritten rather than
-- accumulated; with it, a re-run inside the same week collapses onto itself
-- (idempotent) while a new week inserts a new row.
--
-- Safe to add to the populated table: every pre-existing row has
-- source_item_id IS NULL, and NULLs compare distinct.
CREATE UNIQUE INDEX IF NOT EXISTS uq_deals_source_item
    ON deals (store_id, source, source_item_id, valid_from);

CREATE INDEX IF NOT EXISTS idx_deals_source ON deals (source);

COMMIT;

-- Note for whoever lands the Unit 4 conversion of deals.discount_percentage to
-- GENERATED ALWAYS AS (...) STORED (design log 0.7): ingestion currently writes
-- that column explicitly, and an INSERT naming a generated column is an error.
-- app/services/flipp_ingestion.py::_DEAL_UPSERT must drop the column from its
-- INSERT and its ON CONFLICT DO UPDATE in the same migration.
--
-- Rollback:
--   BEGIN;
--   DROP INDEX IF EXISTS idx_deals_source;
--   DROP INDEX IF EXISTS uq_deals_source_item;
--   ALTER TABLE deals DROP COLUMN IF EXISTS source_item_id;
--   ALTER TABLE deals DROP COLUMN IF EXISTS source;
--   COMMIT;
--
-- Reversible, but NOT without consequence: after the rollback, ingested deals are
-- indistinguishable from curated ones, so the dietary gate fails OPEN and every
-- restricted user starts seeing raw flyer names. Delete WHERE source = 'flipp'
-- first, or do not roll this back while ingestion is enabled.
