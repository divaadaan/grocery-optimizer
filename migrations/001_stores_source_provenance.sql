-- 001_stores_source_provenance.sql
-- Why: deal ingestion creates one stores row per flyer merchant per postal code,
--   and needs a natural key to find-or-create it idempotently. The origin schema
--   has no unique constraint on stores at all, so a re-run would duplicate every
--   merchant. source also keeps machine-ingested merchants distinguishable from
--   the hand-written seed stores, which the dietary gate depends on.
-- Design log: (new) "Ingested stores are merchants, not locations" - to be added
--   to ../sql4e/DESIGN-LOG.md alongside 0.3.
-- Hernandez: field-level integrity - a field holds one fact, and provenance is a
--   different fact from identity; plus the table-level rule that every table
--   needs a candidate key other than its surrogate id.

BEGIN;

-- 'seed'   = scripts/seed_sample_data.sql (real addresses, hand written)
-- 'manual' = hand-entered rows
-- 'flipp'  = app/services/flipp_ingestion.py
ALTER TABLE stores
    ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'seed';

-- The provider's own merchant id. NULL for every pre-existing row, which is what
-- makes the unique index below safe to add to a populated table: NULLs compare
-- distinct, so seed rows can never collide with each other or with ingested ones.
ALTER TABLE stores
    ADD COLUMN IF NOT EXISTS source_merchant_id VARCHAR(64);

COMMENT ON COLUMN stores.source IS
    'Provenance: seed | manual | flipp. Deal sources outside app.services.deal_gate.CURATED_SOURCES are not trusted for dietary compliance.';
COMMENT ON COLUMN stores.source_merchant_id IS
    'Provider-side merchant id; NULL for hand-written rows. An ingested store is a merchant within a postal code, not a physical location - flyer items carry no address.';

-- Natural key for the ingestion upsert. Deliberately NOT partial: the NULLs in
-- source_merchant_id already keep every curated row out of the way, and a plain
-- index lets INSERT .. ON CONFLICT infer it without repeating a WHERE clause.
CREATE UNIQUE INDEX IF NOT EXISTS uq_stores_source_merchant
    ON stores (source, source_merchant_id, postal_code);

CREATE INDEX IF NOT EXISTS idx_stores_source ON stores (source);

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP INDEX IF EXISTS idx_stores_source;
--   DROP INDEX IF EXISTS uq_stores_source_merchant;
--   ALTER TABLE stores DROP COLUMN IF EXISTS source_merchant_id;
--   ALTER TABLE stores DROP COLUMN IF EXISTS source;
--   COMMIT;
--
-- Reversible, but the rollback DOES lose data: any store row created by
-- ingestion survives the rollback as an unattributable duplicate of a merchant,
-- with no way left to tell it apart from a curated store. Delete
-- WHERE source = 'flipp' before rolling back if that matters.
