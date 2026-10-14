-- 007_add_product_aliases.sql
-- Why: one product arrives under many strings. Measured live, a single chicken
--   breast produced three series ("Boneless, Skinless Chicken Breasts",
--   "Fresh Boneless, Skinless Chicken Breas", "Maple Leaf Prime raised without
--   antibi"), and three LACTANTIA rows at one price survived dedupe because
--   their truncated names differ by a few characters. Normalisation alone
--   cannot collapse those -- they are genuinely different strings. A
--   many-to-one alias table can, which is also what makes
--   app/services/price_baseline.py usable: it needs 4 distinct weeks on one
--   series, and exact-name matching almost never gets there.
-- Design log: (new) "product_aliases: many ingest strings, one product".
-- Hernandez: resolving a multi-valued dependency into its own relation.

BEGIN;

CREATE TABLE IF NOT EXISTS product_aliases (
    alias_id        SERIAL PRIMARY KEY,

    -- Output of app/services/products.normalize_name(). The uniqueness
    -- constraint is on this, not on the raw string: two raw strings that
    -- normalise identically are the same alias, and the second sighting must
    -- not create a duplicate row.
    normalized_name VARCHAR(200) NOT NULL,
    raw_example     VARCHAR(255) NOT NULL,

    product_id      INTEGER REFERENCES products(product_id) ON DELETE CASCADE,

    status          VARCHAR(16) NOT NULL DEFAULT 'proposed',
    source          VARCHAR(40) NOT NULL,
    confidence      NUMERIC(3, 2),
    proposal_reason TEXT,
    times_seen      INTEGER NOT NULL DEFAULT 1,
    proposed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by     VARCHAR(80),
    reviewed_at     TIMESTAMPTZ
);

ALTER TABLE product_aliases
    DROP CONSTRAINT IF EXISTS chk_product_aliases_status;
ALTER TABLE product_aliases
    ADD CONSTRAINT chk_product_aliases_status
    CHECK (status IN ('proposed', 'confirmed', 'rejected'));

-- A confirmed alias must point somewhere and name its reviewer. A proposed or
-- rejected one may have a NULL product_id ("the model could not place this").
ALTER TABLE product_aliases
    DROP CONSTRAINT IF EXISTS chk_product_aliases_confirmed_complete;
ALTER TABLE product_aliases
    ADD CONSTRAINT chk_product_aliases_confirmed_complete
    CHECK (
        status <> 'confirmed'
        OR (product_id IS NOT NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)
    );

ALTER TABLE product_aliases
    DROP CONSTRAINT IF EXISTS chk_product_aliases_confidence;
ALTER TABLE product_aliases
    ADD CONSTRAINT chk_product_aliases_confidence
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1));

-- One row per normalised string. Re-proposing a name bumps times_seen via
-- ON CONFLICT instead of inserting a second row, so the review queue does not
-- fill with the same item every week.
CREATE UNIQUE INDEX IF NOT EXISTS uq_product_aliases_normalized
    ON product_aliases (normalized_name);

CREATE INDEX IF NOT EXISTS idx_product_aliases_status ON product_aliases (status);
CREATE INDEX IF NOT EXISTS idx_product_aliases_product ON product_aliases (product_id);

COMMENT ON TABLE product_aliases IS
    'Maps normalised ingest strings onto canonical products, many-to-one. Only status=''confirmed'' rows are used for linking; proposed rows are a review queue and affect nothing.';
COMMENT ON COLUMN product_aliases.times_seen IS
    'How many ingestion runs have encountered this string. A high count on a still-proposed alias means a frequently-advertised item is being withheld from restricted users and should be reviewed first.';

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP TABLE IF EXISTS product_aliases;
--   COMMIT;
--
-- Reversible. Dropping it loses the curated alias mappings, which are real work
-- but are reproducible by re-running propose + review.
