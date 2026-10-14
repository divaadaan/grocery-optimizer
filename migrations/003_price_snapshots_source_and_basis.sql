-- 003_price_snapshots_source_and_basis.sql
-- Why: price_snapshots was dead-but-ready schema -- well indexed, including a
--   trigram GIN on product_name, and referenced by no Python at all. Ingestion
--   now writes it so the project accumulates its own price history instead of
--   depending on deals.regular_price, which the flyer source populates on only
--   ~14% of items. Three things are missing before it can be written honestly:
--   provenance, a natural key so a second run on the same day does not
--   double-weight that day in the median, and a way to say WHICH price we saw.
-- Design log: (new) "price_snapshots gets a writer, and a price_basis column".
-- Hernandez: field-level integrity - price and sale_price each record one fact,
--   and a NOT NULL on a fact the source does not supply forces a lie. The fix is
--   to allow the NULL and add a field that records what was actually observed.

BEGIN;

-- Provenance, mirroring 001/002.
ALTER TABLE price_snapshots
    ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'seed';

ALTER TABLE price_snapshots
    ADD COLUMN IF NOT EXISTS source_item_id VARCHAR(64);

-- Which number(s) this observation actually contains.
--   'sale_only'         -> sale_price is the advertised promo price; price IS NULL
--                          because the source sent no pre-promo price. The common
--                          case: ~86% of observed flyer items.
--   'regular_and_sale'  -> price is the pre-promo shelf price AND sale_price is
--                          the promo price. Both real, both from the source.
--   'regular_only'      -> price is a shelf price with no promotion attached.
--                          Reserved for a future non-flyer source; ingestion does
--                          not emit it today.
--   'unknown'           -> pre-existing rows, whose provenance we cannot recover.
-- The point of the column is that "we only got one number" and "the shelf price
-- happens to equal the sale price" are different facts, and guessing between them
-- silently corrupts every savings figure derived downstream.
ALTER TABLE price_snapshots
    ADD COLUMN IF NOT EXISTS price_basis VARCHAR(20) NOT NULL DEFAULT 'unknown';

-- The origin schema declares price NOT NULL. That forces a caller with only a
-- promo price to invent a shelf price, which is precisely the fabrication this
-- change exists to prevent. Drop the constraint; price_basis now carries the
-- information the NOT NULL was pretending to guarantee.
ALTER TABLE price_snapshots
    ALTER COLUMN price DROP NOT NULL;

ALTER TABLE price_snapshots
    DROP CONSTRAINT IF EXISTS chk_price_snapshots_price_basis;
ALTER TABLE price_snapshots
    ADD CONSTRAINT chk_price_snapshots_price_basis
    CHECK (price_basis IN ('sale_only', 'regular_and_sale', 'regular_only', 'unknown'));

COMMENT ON COLUMN price_snapshots.price IS
    'Shelf / pre-promo price. NULL when the source supplied only a promo price - see price_basis. Nullable since migration 003.';
COMMENT ON COLUMN price_snapshots.sale_price IS
    'Advertised promotional price. For flyer-sourced rows this is the only price that is always present.';
COMMENT ON COLUMN price_snapshots.price_basis IS
    'Which prices this row actually observed: sale_only | regular_and_sale | regular_only | unknown. Read app/services/price_baseline.py before computing savings from these rows - a median over flyer data is a median of SALE prices, not a shelf price.';

-- One observation per item per day. time is truncated to UTC midnight by
-- app/services/flipp_ingestion.py, so two refreshes on the same day collapse
-- instead of giving that day double weight in the trailing median.
--
-- time is included because a unique index on a partitioned table must contain
-- every partition-key column. Safe to add to the populated table: pre-existing
-- rows all have source_item_id IS NULL, and NULLs compare distinct.
CREATE UNIQUE INDEX IF NOT EXISTS uq_price_snapshots_source_item_day
    ON price_snapshots (store_id, source, source_item_id, time);

CREATE INDEX IF NOT EXISTS idx_price_snapshots_source ON price_snapshots (source);

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP INDEX IF EXISTS idx_price_snapshots_source;
--   DROP INDEX IF EXISTS uq_price_snapshots_source_item_day;
--   ALTER TABLE price_snapshots DROP CONSTRAINT IF EXISTS chk_price_snapshots_price_basis;
--   DELETE FROM price_snapshots WHERE price IS NULL;   -- <-- DESTRUCTIVE
--   ALTER TABLE price_snapshots ALTER COLUMN price SET NOT NULL;
--   ALTER TABLE price_snapshots DROP COLUMN IF EXISTS price_basis;
--   ALTER TABLE price_snapshots DROP COLUMN IF EXISTS source_item_id;
--   ALTER TABLE price_snapshots DROP COLUMN IF EXISTS source;
--   COMMIT;
--
-- NOT REVERSIBLE WITHOUT DATA LOSS. Restoring price NOT NULL requires deleting
-- every sale_only observation, which is ~86% of all ingested history and the
-- entire basis of app/services/price_baseline.py. The alternative - backfilling
-- price from sale_price - would restore the constraint by writing promo prices
-- into a shelf-price column, i.e. fabricating the exact data this migration was
-- written to stop fabricating. Do not do that.
