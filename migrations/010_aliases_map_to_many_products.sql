-- 010_aliases_map_to_many_products.sql
-- Why: 009 gave a DEAL many products, but an ALIAS still cannot have more than
--   one. `uq_product_aliases_normalized` is unique on normalized_name alone,
--   so the string
--     "ANNIE'S SHELLS & CHEDDAR 170 G BEATRICE CHOCOLATE MILK 1 L
--      GOLDFISH CRACKERS 168 - 227 G"
--   can be mapped to mac-and-cheese OR chocolate milk OR crackers, never all
--   three. That single constraint is the last thing blocking decomposition.
-- Design log: (new) "one tile string, several products".
-- Hernandez: the alias relation's real key is the pairing, not the name --
--   07's uniqueness was a simplification that held only while every string
--   described one product.
--
-- Two partial indexes replace the one total index, because the two cases have
-- genuinely different rules:
--   * placed    -> (normalized_name, product_id) unique, so one name maps to
--                  many products but never twice to the same one.
--   * unplaced  -> (normalized_name) unique where product_id IS NULL, so a
--                  string the labeller could not place still gets exactly one
--                  row. Without this, plain (normalized_name, product_id)
--                  would let unlimited NULL rows accumulate, since NULLs
--                  compare distinct -- one new review-queue entry per
--                  ingestion run, forever.

BEGIN;

DROP INDEX IF EXISTS uq_product_aliases_normalized;

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_aliases_name_product
    ON product_aliases (normalized_name, product_id)
    WHERE product_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_product_aliases_name_unplaced
    ON product_aliases (normalized_name)
    WHERE product_id IS NULL;

-- Which product within the tile this alias refers to, and whether the deal's
-- sale_price is this product's own price. Mirrors deal_products so the link
-- step can carry both through without re-deciding anything.
ALTER TABLE product_aliases
    ADD COLUMN IF NOT EXISTS tile_position SMALLINT NOT NULL DEFAULT 0;

-- Nullable here, unlike deal_products.price_is_per_item: at labelling time the
-- answer can genuinely be unknown ("2 for $5" with no unit recorded), and
-- forcing a value would mean guessing. NULL means "undecided", and the link
-- step refuses to create a deal_products row from it rather than defaulting.
ALTER TABLE product_aliases
    ADD COLUMN IF NOT EXISTS price_is_per_item BOOLEAN;

COMMENT ON COLUMN product_aliases.tile_position IS
    'Position within a multi-product flyer tile; 0 for an ordinary single-product string.';
COMMENT ON COLUMN product_aliases.price_is_per_item IS
    'Whether the deal''s sale_price is this product''s own price. NULL means undecided - the link step will not guess, and a NULL blocks the junction row rather than defaulting to true.';
COMMENT ON INDEX uq_product_aliases_name_product IS
    'One name may map to many products, but never twice to the same product.';
COMMENT ON INDEX uq_product_aliases_name_unplaced IS
    'Exactly one unplaced row per name, so a string the labeller could not place does not re-queue on every ingestion run.';

COMMIT;

-- Rollback:
--   BEGIN;
--   ALTER TABLE product_aliases DROP COLUMN IF EXISTS price_is_per_item;
--   ALTER TABLE product_aliases DROP COLUMN IF EXISTS tile_position;
--   DROP INDEX IF EXISTS uq_product_aliases_name_unplaced;
--   DROP INDEX IF EXISTS uq_product_aliases_name_product;
--   -- This fails if any name now maps to more than one product:
--   CREATE UNIQUE INDEX uq_product_aliases_normalized
--       ON product_aliases (normalized_name);
--   COMMIT;
--
-- NOT REVERSIBLE once a tile has been decomposed. Restoring single-column
-- uniqueness requires deleting all but one alias per name, which discards the
-- decomposition -- curated work, and the whole point of the migration. Delete
-- the surplus rows deliberately if you really mean to go back.
