-- 006_add_products.sql
-- Why: dietary compliance currently depends on casefold-substring matching the
--   raw flyer string (app/agents/dietary.py), which real product names break in
--   the unsafe direction -- "prosciutto", "gelatin", "lard" and "marzipan"
--   contain no forbidden term. This table moves compliance onto curated DATA
--   about a canonical product instead of inference over an ingest string.
--   Attributes are booleans, not a free-form taxonomy, because the app supports
--   exactly four restriction labels and each maps onto a conjunction of these.
-- Design log: (new) "products: compliance becomes data, not string matching".
-- Hernandez: single-field-value rule -- one fact per field, and the fact is
--   about the product, not about the words a flyer happened to print.

BEGIN;

CREATE TABLE IF NOT EXISTS products (
    product_id       SERIAL PRIMARY KEY,
    canonical_name   VARCHAR(120) NOT NULL,
    category         VARCHAR(50)  NOT NULL,

    -- Allergen / diet attributes. NOT NULL on purpose: "we don't know" is not
    -- a value here. An unknown product has no confirmed row at all, and the
    -- gate excludes it -- that is the fail-closed path. A nullable column
    -- would let "unknown" be silently read as "safe".
    contains_meat    BOOLEAN NOT NULL,   -- incl. poultry; drives vegan/vegetarian/pescatarian
    contains_fish    BOOLEAN NOT NULL,   -- incl. shellfish; drives vegan/vegetarian
    contains_dairy   BOOLEAN NOT NULL,   -- drives vegan
    contains_egg     BOOLEAN NOT NULL,   -- drives vegan
    contains_nuts    BOOLEAN NOT NULL,   -- drives no_nuts / nut_free
    is_pantry_staple BOOLEAN NOT NULL DEFAULT false,

    -- Review provenance. Nothing an LLM proposes is load-bearing until a human
    -- moves it to 'confirmed'; see app/services/product_proposer.py.
    status           VARCHAR(16) NOT NULL DEFAULT 'proposed',
    proposed_by      VARCHAR(40),
    proposal_reason  TEXT,
    reviewed_by      VARCHAR(80),
    reviewed_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE products
    DROP CONSTRAINT IF EXISTS chk_products_status;
ALTER TABLE products
    ADD CONSTRAINT chk_products_status
    CHECK (status IN ('proposed', 'confirmed', 'rejected'));

-- A confirmed row must say who confirmed it. Keeps "confirmed" from being
-- reachable by an automated path that forgot to record a reviewer.
ALTER TABLE products
    DROP CONSTRAINT IF EXISTS chk_products_confirmed_has_reviewer;
ALTER TABLE products
    ADD CONSTRAINT chk_products_confirmed_has_reviewer
    CHECK (status <> 'confirmed' OR (reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL));

-- Canonical names are unique among live rows only. A rejected proposal for
-- "chicken breast" must not block the real one from being created later.
CREATE UNIQUE INDEX IF NOT EXISTS uq_products_canonical_name_live
    ON products (lower(canonical_name))
    WHERE status <> 'rejected';

CREATE INDEX IF NOT EXISTS idx_products_status ON products (status);
CREATE INDEX IF NOT EXISTS idx_products_category ON products (category);

COMMENT ON TABLE products IS
    'Canonical grocery products with curated dietary attributes. Compliance reads these columns; it never infers from a product name. Only status=''confirmed'' rows may affect what a restricted user is served.';
COMMENT ON COLUMN products.contains_fish IS
    'True for fish AND shellfish. The app has no separate shellfish restriction label, so they share a column rather than inventing an attribute nothing reads.';
COMMENT ON COLUMN products.status IS
    'proposed -> an LLM or import suggested this and NO user-facing behaviour depends on it; confirmed -> a human verified the attributes; rejected -> kept for audit so the same bad proposal is not re-queued.';

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP TABLE IF EXISTS products;   -- safe only before 007/008 add FKs to it
--   COMMIT;
--
-- Reversible while no other table references products. Once 008 adds
-- deals.product_id / price_snapshots.product_id, roll those back first or the
-- DROP fails on the dependency.
