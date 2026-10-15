-- 009_deal_products_junction.sql
-- Why: a flyer tile can advertise several unrelated products at one price, and
--   the provider returns it as one item with one name and one price. Ingestion
--   stores it as one deal row, so
--     "ANNIE'S SHELLS & CHEDDAR 170 G BEATRICE CHOCOLATE MILK 1 L
--      GOLDFISH CRACKERS 168 - 227 G"
--   is a single unusable row: it matches no ingredient, cannot be priced, and
--   can only be withheld. 28 of 91 ingested names (31%) carry a multi-product
--   signal, so this is a third of the catalogue rather than an edge case.
--   See app/tests/test_multiproduct_lineitems.py and the ROADMAP debt entry.
--
--   One deal relating to many products is a multi-valued dependency, so it
--   resolves into its own relation. The earlier debt note claimed this would
--   force a change to the deals natural key
--   (store_id, source, source_item_id, valid_from) because one tile has one
--   source_item_id -- that was wrong, and only true for the alternative design
--   of splitting a tile into N deal rows. A junction needs no key change, no
--   row duplication, and leaves ingestion idempotency untouched.
-- Design log: (new) "deal_products: one tile, many products".
-- Hernandez: resolving a multi-valued dependency into its own relation -- the
--   same rule 007 applied to product_aliases.

BEGIN;

CREATE TABLE IF NOT EXISTS deal_products (
    deal_id    INTEGER NOT NULL REFERENCES deals(deal_id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,

    -- Position within the tile, so "the first product listed" is recoverable.
    -- 0 for a plain single-product deal.
    tile_position SMALLINT NOT NULL DEFAULT 0,

    -- Does sale_price apply to THIS product on its own?
    --   true  -> the tile priced each item separately ("$4.96 each"), so this
    --            product really is available at sale_price. `unit` carries the
    --            signal: 11 of the 17 known tiles say each / ea. / lb.
    --   false -> the price covers a bundle or a multi-buy ("2 for $5"), so
    --            sale_price is NOT this product's unit price and nothing
    --            downstream may treat it as one.
    -- NOT NULL with no default on purpose: whoever inserts a row has to have
    -- decided. A default would let "we never worked it out" masquerade as
    -- "each", which is exactly how a wrong price reaches a shopping list.
    price_is_per_item BOOLEAN NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (deal_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_deal_products_product ON deal_products (product_id);

COMMENT ON TABLE deal_products IS
    'Which canonical products a deal actually advertises. One row for an ordinary deal, several for a multi-product flyer tile. AUTHORITATIVE for compliance: app/services/deal_gate.py reads this, not deals.product_id.';
COMMENT ON COLUMN deal_products.price_is_per_item IS
    'True when deals.sale_price is this product''s own price. False for bundle or multi-buy pricing, where sale_price must not be read as a unit price.';
COMMENT ON COLUMN deals.product_id IS
    'Legacy single-product link, kept because the link step and existing rows use it. deal_products is authoritative once populated; prefer it for anything new.';

-- Backfill from the links the review pass already produced, so the junction
-- starts complete rather than empty. Existing rows are single-product by
-- construction, hence position 0 and per-item pricing.
INSERT INTO deal_products (deal_id, product_id, tile_position, price_is_per_item)
SELECT d.deal_id, d.product_id, 0, true
FROM deals d
WHERE d.product_id IS NOT NULL
ON CONFLICT (deal_id, product_id) DO NOTHING;

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP TABLE IF EXISTS deal_products;
--   COMMIT;
--
-- Reversible while nothing but the backfill is in it: every row is derivable
-- from deals.product_id. It stops being reversible once a tile has been
-- decomposed into several products, because deals.product_id can hold only one
-- of them -- at that point dropping the table loses the decomposition, which is
-- real curated work. Re-running propose + review would rebuild it.
