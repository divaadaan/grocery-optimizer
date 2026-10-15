-- 011_deals_price_semantics_text.sql
-- Why: 010 left the undecided-price tiles withheld. `deal_products.price_is_per_item`
--   has to be true or false before a tile can be linked, and the only signal
--   persisted today is `deals.unit` -- "each", "ea.", "lb" mean the tile prices
--   items separately. That decided 11 of 17 known tiles; the other 6 had no
--   unit and stayed null, which blocks the link by design rather than guessing.
--
--   The provider does send the missing signal. `sale_story` is free text like
--   "save $10", "SAVE 30%", "2 FOR $5", "Rollback"; `pre_price_text` is "NOW",
--   "from", "Online Price". A multi-buy story means the price is NOT any one
--   product's price, and a "from" prefix means it is a floor rather than a
--   price. Ingestion already reads sale_story into NormalizedDeal and then
--   throws it away at the INSERT; pre_price_text it never read at all.
--
--   So this is not new data collection -- it is stopping the discard of data
--   already fetched over the wire.
-- Design log: (new) "persisting the price-semantics text".
-- Hernandez: do not derive-and-discard. The raw field is the record of what the
--   source said; an inference drawn from it belongs beside it, not instead of
--   it (same reasoning as keeping product_name next to product_id).

BEGIN;

ALTER TABLE deals
    ADD COLUMN IF NOT EXISTS sale_story VARCHAR(255);

ALTER TABLE deals
    ADD COLUMN IF NOT EXISTS pre_price_text VARCHAR(100);

COMMENT ON COLUMN deals.sale_story IS
    'Promotional free text as printed, e.g. "save $10", "SAVE 30%", "2 FOR $5", "Rollback". Read by app/services/flipp_ingestion.price_is_per_item to tell multi-buy pricing from per-item pricing. Kept raw: the inference can be revised, the source text cannot be recovered.';
COMMENT ON COLUMN deals.pre_price_text IS
    'Price prefix as printed, e.g. "NOW", "from", "Online Price". A "from" prefix means sale_price is a floor across variants, not a single price.';

CREATE INDEX IF NOT EXISTS idx_deals_sale_story
    ON deals (sale_story)
    WHERE sale_story IS NOT NULL;

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP INDEX IF EXISTS idx_deals_sale_story;
--   ALTER TABLE deals DROP COLUMN IF EXISTS pre_price_text;
--   ALTER TABLE deals DROP COLUMN IF EXISTS sale_story;
--   COMMIT;
--
-- Reversible. Dropping these loses the promo text for rows already ingested,
-- which is only recoverable by re-ingesting while the same flyers are live --
-- so it is lossy in practice even though no derived data depends on it yet.
