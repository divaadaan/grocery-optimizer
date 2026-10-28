-- 016_product_attribute_corrections.sql
-- Why: a confirmed product's attributes are the allergen authority (deal_gate
--   serves from them, agents/dietary.py judges recipes by them), and since
--   PR #24 the review flow refuses to change them. A confirmed value can still
--   be wrong, so correcting one is a separate, deliberate act
--   (POST /admin/products/{id}/attribute-corrections) -- and an act that
--   changes what restricted users are served needs a durable record of who,
--   when, what, from what, to what, and why. These tables are that record.
-- Design log: (new) "product_corrections: audited changes to curated products".
-- Hernandez: one fact per row -- a correction EVENT (who, when, why, which
--   product) is one relation; each attribute it changed (old -> new) is a
--   fact about that event, so it resolves into its own detail relation rather
--   than repeating the reviewer and reason on every attribute row.
--
-- Shape, and why it is two tables:
--   product_corrections            one row per correction event, any kind.
--   product_attribute_corrections  one row per attribute an event changed.
--   Renames/recategorisations and merges (a follow-up) are corrections too,
--   with the same who/when/why/product header but differently-shaped details
--   (old/new name and category; the absorbed product and what moved). They
--   get their own `kind` value and their own sibling detail table hanging off
--   product_corrections, so this table never grows nullable columns for other
--   kinds and old_value/new_value stay typed BOOLEAN, not text-encoded. The
--   kind CHECK lists only 'attribute' today; the follow-up widens it.
--
-- Append-only, enforced: UPDATE, DELETE and TRUNCATE on either table raise.
--   An audit row that can be edited is not an audit row. This is a guardrail,
--   not security -- a superuser can disable a trigger -- and the reviewer is a
--   client-asserted string (the admin routes have no authentication).
-- products is referenced ON DELETE RESTRICT: a product with correction
--   history cannot be deleted out from under it (retire it via status).
-- impact JSONB stores the report the endpoint returned at the time (deals and
--   users whose verdict changed), so "who was affected" survives later edits.
--
-- Must apply after 015 (reads nothing from it, but the app code that writes
--   here iterates products.ATTRIBUTES, which names 015's column). Not applied
--   anywhere by this change.

BEGIN;

CREATE TABLE IF NOT EXISTS product_corrections (
    correction_id  BIGSERIAL PRIMARY KEY,
    product_id     INTEGER NOT NULL
                   REFERENCES products(product_id) ON DELETE RESTRICT,
    kind           VARCHAR(24) NOT NULL,
    reviewer       VARCHAR(80) NOT NULL,
    reason         TEXT NOT NULL,
    impact         JSONB,
    corrected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_product_corrections_kind
        CHECK (kind IN ('attribute')),
    CONSTRAINT chk_product_corrections_reviewer
        CHECK (btrim(reviewer) <> ''),
    CONSTRAINT chk_product_corrections_reason
        CHECK (btrim(reason) <> '')
);

CREATE INDEX IF NOT EXISTS idx_product_corrections_product
    ON product_corrections (product_id, corrected_at DESC);

CREATE TABLE IF NOT EXISTS product_attribute_corrections (
    correction_id  BIGINT NOT NULL
                   REFERENCES product_corrections(correction_id) ON DELETE RESTRICT,
    -- A products column name, validated against products.ATTRIBUTES by the
    -- app. Text, not an enum: a new attribute (e.g. contains_gluten) must not
    -- need a migration here.
    attribute      VARCHAR(64) NOT NULL,
    -- NULL old_value = the attribute was undecided (migration 015's nullable
    -- column). new_value is never NULL: a correction lands a decision.
    old_value      BOOLEAN,
    new_value      BOOLEAN NOT NULL,

    PRIMARY KEY (correction_id, attribute),
    CONSTRAINT chk_product_attribute_corrections_changes
        CHECK (old_value IS DISTINCT FROM new_value)
);

COMMENT ON TABLE product_corrections IS
    'Append-only log of deliberate corrections to confirmed products, one row per event. kind names the sibling detail table (attribute -> product_attribute_corrections). reviewer is client-asserted (no auth). impact is the endpoint''s impact report at the time.';
COMMENT ON TABLE product_attribute_corrections IS
    'Append-only: the attributes one product_corrections event changed, old -> new. Applied to products in the same transaction as the header row.';

-- Shared by every audit table, so the follow-up's sibling tables reuse it.
CREATE OR REPLACE FUNCTION forbid_audit_row_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % refused', TG_TABLE_NAME, TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS trg_product_corrections_append_only
    ON product_corrections;
CREATE TRIGGER trg_product_corrections_append_only
    BEFORE UPDATE OR DELETE ON product_corrections
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_row_change();

DROP TRIGGER IF EXISTS trg_product_corrections_no_truncate
    ON product_corrections;
CREATE TRIGGER trg_product_corrections_no_truncate
    BEFORE TRUNCATE ON product_corrections
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_row_change();

DROP TRIGGER IF EXISTS trg_product_attribute_corrections_append_only
    ON product_attribute_corrections;
CREATE TRIGGER trg_product_attribute_corrections_append_only
    BEFORE UPDATE OR DELETE ON product_attribute_corrections
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_row_change();

DROP TRIGGER IF EXISTS trg_product_attribute_corrections_no_truncate
    ON product_attribute_corrections;
CREATE TRIGGER trg_product_attribute_corrections_no_truncate
    BEFORE TRUNCATE ON product_attribute_corrections
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_row_change();

-- The flat "who changed which attribute on what, when, and why" view, for
-- reading the log without remembering the join.
CREATE OR REPLACE VIEW product_attribute_correction_log AS
SELECT c.correction_id,
       c.corrected_at,
       c.reviewer,
       c.product_id,
       p.canonical_name,
       a.attribute,
       a.old_value,
       a.new_value,
       c.reason
FROM product_corrections c
JOIN product_attribute_corrections a ON a.correction_id = c.correction_id
JOIN products p ON p.product_id = c.product_id
WHERE c.kind = 'attribute';

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP VIEW IF EXISTS product_attribute_correction_log;
--   DROP TABLE IF EXISTS product_attribute_corrections;
--   DROP TABLE IF EXISTS product_corrections;
--   DROP FUNCTION IF EXISTS forbid_audit_row_change();
--   COMMIT;
--
-- Roll back the CODE first: the correction endpoint writes these tables, and
-- once they are gone every correction fails (it fails closed -- the attribute
-- UPDATE is in the same transaction, so nothing changes unaudited). Not
-- reversible without data loss: dropping the tables loses the audit history.
-- The attribute values the corrections set stay on products; only the record
-- of who changed them and why is lost. The triggers do not block DROP TABLE.
