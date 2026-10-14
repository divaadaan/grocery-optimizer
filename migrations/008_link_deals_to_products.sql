-- 008_link_deals_to_products.sql
-- Why: deals and price_snapshots need to point at a canonical product so
--   compliance can read curated attributes and the baseline can aggregate one
--   series instead of many near-identical strings. product_name STAYS as the
--   raw ingest string per migrations/README.md rule 3 -- this is additive, and
--   the raw string remains the record of what the flyer actually printed.
-- Design log: (new) "products: compliance becomes data, not string matching".
-- Hernandez: additive-first -- add the foreign key and leave the original
--   field in place; nothing is dropped while anything still reads it.

BEGIN;

-- Nullable on purpose. NULL means "not yet linked to a confirmed product",
-- which is the normal state for a newly ingested item and is exactly what the
-- review queue is built from. It must never be read as "safe".
ALTER TABLE deals
    ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES products(product_id) ON DELETE SET NULL;

ALTER TABLE price_snapshots
    ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES products(product_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_deals_product ON deals (product_id);
CREATE INDEX IF NOT EXISTS idx_price_snapshots_product_id ON price_snapshots (product_id);

COMMENT ON COLUMN deals.product_id IS
    'Canonical product, or NULL when no confirmed alias matches the normalised product_name. NULL is not "safe" - app/services/deal_gate.py excludes unlinked deals from users with dietary restrictions.';
COMMENT ON COLUMN price_snapshots.product_id IS
    'Canonical product for aggregation. app/services/price_baseline.py prefers this over product_name: a trailing median over one product beats a median over one exact string.';

-- The review queue, as a view rather than a table: it is derivable, and a
-- second copy of this would drift from deals the moment anything was deleted.
-- Ordered by how often the string shows up, so the most-withheld items surface
-- first.
CREATE OR REPLACE VIEW unmapped_deal_names AS
SELECT
    d.product_name,
    d.source,
    count(*)                                   AS deal_count,
    count(DISTINCT d.store_id)                 AS store_count,
    min(d.sale_price)                          AS min_sale_price,
    max(d.sale_price)                          AS max_sale_price,
    max(d.valid_until)                         AS last_valid_until,
    bool_or(d.valid_until >= CURRENT_DATE)     AS currently_valid
FROM deals d
WHERE d.product_id IS NULL
GROUP BY d.product_name, d.source;

COMMENT ON VIEW unmapped_deal_names IS
    'Deal names with no confirmed product link - the propose/review queue. A staple that first goes on sale months from now appears here automatically on the next ingestion run; nothing has to be curated up front.';

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP VIEW IF EXISTS unmapped_deal_names;
--   DROP INDEX IF EXISTS idx_price_snapshots_product_id;
--   DROP INDEX IF EXISTS idx_deals_product;
--   ALTER TABLE price_snapshots DROP COLUMN IF EXISTS product_id;
--   ALTER TABLE deals DROP COLUMN IF EXISTS product_id;
--   COMMIT;
--
-- Reversible. Dropping the columns loses the links but no ingest data: every
-- product_name is untouched, so re-running link rebuilds them.
