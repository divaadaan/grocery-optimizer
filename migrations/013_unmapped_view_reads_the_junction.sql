-- 013_unmapped_view_reads_the_junction.sql
-- Why: the review queue never drains. `unmapped_deal_names` (migration 008)
--   keys off `d.product_id IS NULL`, but `link_deals` *deliberately* leaves
--   that column NULL for a multi-product flyer tile, because one column cannot
--   hold three answers. So a tile that is fully linked through deal_products
--   still reports as unmapped, re-exports on every labelling pass, and gets
--   re-proposed forever. The 5-term probe measured 13 such names (the
--   Lactantia, Neilson and Annie's tiles).
--
--   This is the same mistake commit 27d1196 fixed in `review_stats` -- counting
--   the legacy single-product column instead of the junction -- and it was not
--   fixed in this view at the same time. Two readers of "is this linked?"
--   disagreeing is the actual defect; the junction is authoritative.
-- Design log: (new) "the unmapped view reads the junction".
-- Hernandez: one fact, one place. "Linked" is recorded in deal_products; any
--   second derivation of it will drift from the first.

BEGIN;

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
-- Unmapped means "no product attached at all", read from the junction. A
-- single-product deal has one row here and a decomposed tile has several;
-- either way the deal is mapped. deals.product_id is not consulted, because a
-- tile leaves it NULL by design.
WHERE NOT EXISTS (
    SELECT 1 FROM deal_products dp WHERE dp.deal_id = d.deal_id
)
GROUP BY d.product_name, d.source;

COMMENT ON VIEW unmapped_deal_names IS
    'Deal names with no product attached in deal_products - the propose/review queue. Reads the junction, NOT deals.product_id: a decomposed multi-product tile leaves that column NULL by design, and keying off it kept fully-linked tiles in the queue forever (13 names, measured 2026-10-15).';

COMMIT;

-- Rollback:
--   BEGIN;
--   CREATE OR REPLACE VIEW unmapped_deal_names AS
--   SELECT d.product_name, d.source, count(*) AS deal_count,
--          count(DISTINCT d.store_id) AS store_count,
--          min(d.sale_price) AS min_sale_price, max(d.sale_price) AS max_sale_price,
--          max(d.valid_until) AS last_valid_until,
--          bool_or(d.valid_until >= CURRENT_DATE) AS currently_valid
--   FROM deals d WHERE d.product_id IS NULL
--   GROUP BY d.product_name, d.source;
--   COMMIT;
--
-- Reversible -- it is a view definition, no data is touched. Rolling back
-- restores the behaviour where decomposed tiles never leave the queue.
