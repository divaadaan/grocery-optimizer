-- 018_product_rename_and_merge.sql
-- Why: two curated-catalogue mistakes have no audited fix. A confirmed
--   product can carry the wrong identity (P84 "cheese tofu" is a fish-paste
--   product: wrong canonical_name, wrong category), and two confirmed rows
--   can be the same product, splitting its aliases and deals between them.
--   Both are corrections to curated products, so they extend 016's audit log
--   (one product_corrections header per event, one sibling detail table per
--   kind) rather than starting a second one.
-- Design log: (new) "product_corrections: rename and merge kinds".
-- Hernandez: one fact per row -- the event header stays in product_corrections;
--   a rename's changed fields and a merge's moved rows are facts about that
--   event, so each resolves into its own detail relation.
--
-- What changes:
--   product_corrections.kind      widened to 'attribute', 'identity', 'merge'.
--   product_identity_corrections  one row per identity field a rename changed
--                                 (canonical_name / category), old -> new.
--   product_merge_corrections     one row per merge: the survivor (the header's
--                                 product_id is the ABSORBED product) and how
--                                 many price snapshots were repointed.
--   product_merge_moves           one row per alias / deal link the merge
--                                 moved, retired or dropped -- "what moved".
--   products.status               gains 'merged'; products.merged_into records
--                                 the survivor. A merged product is never
--                                 deleted (016's ON DELETE RESTRICT forbids it
--                                 anyway) and is never served: every reader
--                                 gates on status = 'confirmed'.
--   uq_products_canonical_name_live  now covers proposed + confirmed only, so
--                                 a merged product no longer holds its name
--                                 (the survivor may take it). The proposer
--                                 redirects a merged name to its survivor.
--   products_merge_guard()        refuses un-merging, merging an unconfirmed
--                                 product, merging into anything but a
--                                 confirmed product, and chains.
--
-- The new detail tables are append-only through 016's shared trigger function
--   forbid_audit_row_change(). Must apply after 016 (uses its tables and
--   function). Independent of 017. Not applied anywhere by this change.
-- The app code on this branch reads products.merged_into in the proposer, so
--   apply 018 before running propose_products.py from this code.

BEGIN;

-- ---------------------------------------------------------------- products

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS merged_into INTEGER
        REFERENCES products(product_id) ON DELETE RESTRICT;

ALTER TABLE products
    DROP CONSTRAINT IF EXISTS chk_products_status;
ALTER TABLE products
    ADD CONSTRAINT chk_products_status
    CHECK (status IN ('proposed', 'confirmed', 'rejected', 'merged'));

-- merged <=> it names a survivor, and never itself.
ALTER TABLE products
    DROP CONSTRAINT IF EXISTS chk_products_merged_into;
ALTER TABLE products
    ADD CONSTRAINT chk_products_merged_into
    CHECK ((status = 'merged') = (merged_into IS NOT NULL)
           AND merged_into IS DISTINCT FROM product_id);

CREATE INDEX IF NOT EXISTS idx_products_merged_into
    ON products (merged_into)
    WHERE merged_into IS NOT NULL;

-- Live names only. 006 excluded rejected rows; a merged row is no more live
-- than a rejected one, and holding its name would stop the survivor being
-- renamed to it.
DROP INDEX IF EXISTS uq_products_canonical_name_live;
CREATE UNIQUE INDEX uq_products_canonical_name_live
    ON products (lower(canonical_name))
    WHERE status IN ('proposed', 'confirmed');

COMMENT ON COLUMN products.status IS
    'proposed -> an LLM or import suggested this and NO user-facing behaviour depends on it; confirmed -> a human verified the attributes; rejected -> kept for audit so the same bad proposal is not re-queued; merged -> a duplicate folded into merged_into (see product_merge_corrections), kept for audit, never served.';
COMMENT ON COLUMN products.merged_into IS
    'The surviving product a merged duplicate was folded into. Set exactly when status = ''merged''; always a confirmed, unmerged product (products_merge_guard).';

-- Belt and braces under the app's row locks: a CHECK cannot look at another
-- row, so chain and target rules live in a trigger.
CREATE OR REPLACE FUNCTION products_merge_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_status TEXT;
BEGIN
    IF OLD.status = 'merged' THEN
        IF NEW.status IS DISTINCT FROM 'merged'
           OR NEW.merged_into IS DISTINCT FROM OLD.merged_into THEN
            RAISE EXCEPTION 'product % was merged into %; a merge is not undone in place',
                OLD.product_id, OLD.merged_into;
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status = 'merged' THEN
        IF OLD.status <> 'confirmed' THEN
            RAISE EXCEPTION 'product % is %, only a confirmed product can be merged',
                OLD.product_id, OLD.status;
        END IF;
        SELECT status INTO target_status
        FROM products WHERE product_id = NEW.merged_into;
        IF target_status IS DISTINCT FROM 'confirmed' THEN
            RAISE EXCEPTION 'merge target % is %, not confirmed',
                NEW.merged_into, COALESCE(target_status, 'missing');
        END IF;
        IF EXISTS (SELECT 1 FROM products WHERE merged_into = NEW.product_id) THEN
            RAISE EXCEPTION 'product % has products merged into it; merging it would chain',
                NEW.product_id;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_products_merge_guard ON products;
CREATE TRIGGER trg_products_merge_guard
    BEFORE UPDATE OF status, merged_into ON products
    FOR EACH ROW EXECUTE FUNCTION products_merge_guard();

-- ------------------------------------------------------- correction kinds

ALTER TABLE product_corrections
    DROP CONSTRAINT IF EXISTS chk_product_corrections_kind;
ALTER TABLE product_corrections
    ADD CONSTRAINT chk_product_corrections_kind
    CHECK (kind IN ('attribute', 'identity', 'merge'));

-- kind = 'identity': a rename and/or recategorisation. Never touches an
-- attribute; an attribute change is a separate 'attribute' event.
CREATE TABLE IF NOT EXISTS product_identity_corrections (
    correction_id  BIGINT NOT NULL
                   REFERENCES product_corrections(correction_id) ON DELETE RESTRICT,
    field          VARCHAR(24) NOT NULL,
    old_value      TEXT NOT NULL,
    new_value      TEXT NOT NULL,

    PRIMARY KEY (correction_id, field),
    CONSTRAINT chk_product_identity_corrections_field
        CHECK (field IN ('canonical_name', 'category')),
    CONSTRAINT chk_product_identity_corrections_changes
        CHECK (old_value IS DISTINCT FROM new_value)
);

-- kind = 'merge': product_corrections.product_id is the ABSORBED product.
CREATE TABLE IF NOT EXISTS product_merge_corrections (
    correction_id        BIGINT PRIMARY KEY
                         REFERENCES product_corrections(correction_id) ON DELETE RESTRICT,
    survivor_product_id  INTEGER NOT NULL
                         REFERENCES products(product_id) ON DELETE RESTRICT,
    -- A count, not ids: price history can run to thousands of rows, and the
    -- repoint is fully described by (absorbed -> survivor) anyway.
    snapshots_repointed  INTEGER NOT NULL DEFAULT 0,

    CONSTRAINT chk_product_merge_corrections_snapshots
        CHECK (snapshots_repointed >= 0)
);

CREATE INDEX IF NOT EXISTS idx_product_merge_corrections_survivor
    ON product_merge_corrections (survivor_product_id);

-- What moved. row_id is product_aliases.alias_id for alias_* actions and
-- deals.deal_id for deal_* actions (deal_products is keyed by the deal plus
-- the absorbed product, which the header already names). detail keeps what a
-- reader needs to reconstruct the row: an alias's old status, a dropped
-- junction row's tile_position / price_is_per_item.
CREATE TABLE IF NOT EXISTS product_merge_moves (
    correction_id  BIGINT NOT NULL
                   REFERENCES product_merge_corrections(correction_id) ON DELETE RESTRICT,
    action         VARCHAR(32) NOT NULL,
    row_id         INTEGER NOT NULL,
    detail         JSONB,

    PRIMARY KEY (correction_id, action, row_id),
    CONSTRAINT chk_product_merge_moves_action
        CHECK (action IN (
            'alias_repointed',        -- product_id absorbed -> survivor
            'alias_retired',          -- survivor already had this string; set rejected
            'deal_link_repointed',    -- deal_products row absorbed -> survivor
            'deal_link_dropped',      -- deal already linked the survivor; row deleted
            'deal_legacy_repointed'   -- deals.product_id absorbed -> survivor
        ))
);

COMMENT ON TABLE product_identity_corrections IS
    'Append-only: the identity fields (canonical_name, category) one product_corrections event of kind identity changed, old -> new. Never an attribute.';
COMMENT ON TABLE product_merge_corrections IS
    'Append-only: one row per merge event. product_corrections.product_id is the absorbed product (now status merged); survivor_product_id received its aliases and deals.';
COMMENT ON TABLE product_merge_moves IS
    'Append-only: every alias and deal link a merge repointed, retired or dropped.';

DROP TRIGGER IF EXISTS trg_product_identity_corrections_append_only
    ON product_identity_corrections;
CREATE TRIGGER trg_product_identity_corrections_append_only
    BEFORE UPDATE OR DELETE ON product_identity_corrections
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_row_change();
DROP TRIGGER IF EXISTS trg_product_identity_corrections_no_truncate
    ON product_identity_corrections;
CREATE TRIGGER trg_product_identity_corrections_no_truncate
    BEFORE TRUNCATE ON product_identity_corrections
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_row_change();

DROP TRIGGER IF EXISTS trg_product_merge_corrections_append_only
    ON product_merge_corrections;
CREATE TRIGGER trg_product_merge_corrections_append_only
    BEFORE UPDATE OR DELETE ON product_merge_corrections
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_row_change();
DROP TRIGGER IF EXISTS trg_product_merge_corrections_no_truncate
    ON product_merge_corrections;
CREATE TRIGGER trg_product_merge_corrections_no_truncate
    BEFORE TRUNCATE ON product_merge_corrections
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_row_change();

DROP TRIGGER IF EXISTS trg_product_merge_moves_append_only
    ON product_merge_moves;
CREATE TRIGGER trg_product_merge_moves_append_only
    BEFORE UPDATE OR DELETE ON product_merge_moves
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_row_change();
DROP TRIGGER IF EXISTS trg_product_merge_moves_no_truncate
    ON product_merge_moves;
CREATE TRIGGER trg_product_merge_moves_no_truncate
    BEFORE TRUNCATE ON product_merge_moves
    FOR EACH STATEMENT EXECUTE FUNCTION forbid_audit_row_change();

-- Flat logs, like 016's product_attribute_correction_log.
CREATE OR REPLACE VIEW product_identity_correction_log AS
SELECT c.correction_id,
       c.corrected_at,
       c.reviewer,
       c.product_id,
       i.field,
       i.old_value,
       i.new_value,
       c.reason
FROM product_corrections c
JOIN product_identity_corrections i ON i.correction_id = c.correction_id
WHERE c.kind = 'identity';

CREATE OR REPLACE VIEW product_merge_log AS
SELECT c.correction_id,
       c.corrected_at,
       c.reviewer,
       c.product_id          AS absorbed_product_id,
       a.canonical_name      AS absorbed_name,
       m.survivor_product_id,
       s.canonical_name      AS survivor_name,
       m.snapshots_repointed,
       (SELECT count(*) FROM product_merge_moves mv
        WHERE mv.correction_id = c.correction_id) AS rows_moved,
       c.reason
FROM product_corrections c
JOIN product_merge_corrections m ON m.correction_id = c.correction_id
JOIN products a ON a.product_id = c.product_id
JOIN products s ON s.product_id = m.survivor_product_id
WHERE c.kind = 'merge';

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP VIEW IF EXISTS product_merge_log;
--   DROP VIEW IF EXISTS product_identity_correction_log;
--   DROP TABLE IF EXISTS product_merge_moves;
--   DROP TABLE IF EXISTS product_merge_corrections;
--   DROP TABLE IF EXISTS product_identity_corrections;
--   ALTER TABLE product_corrections DROP CONSTRAINT IF EXISTS chk_product_corrections_kind;
--   ALTER TABLE product_corrections ADD CONSTRAINT chk_product_corrections_kind
--       CHECK (kind IN ('attribute'));
--   DROP TRIGGER IF EXISTS trg_products_merge_guard ON products;
--   DROP FUNCTION IF EXISTS products_merge_guard();
--   DROP INDEX IF EXISTS uq_products_canonical_name_live;
--   CREATE UNIQUE INDEX uq_products_canonical_name_live
--       ON products (lower(canonical_name)) WHERE status <> 'rejected';
--   ALTER TABLE products DROP CONSTRAINT IF EXISTS chk_products_merged_into;
--   ALTER TABLE products DROP CONSTRAINT IF EXISTS chk_products_status;
--   ALTER TABLE products ADD CONSTRAINT chk_products_status
--       CHECK (status IN ('proposed', 'confirmed', 'rejected'));
--   DROP INDEX IF EXISTS idx_products_merged_into;
--   ALTER TABLE products DROP COLUMN IF EXISTS merged_into;
--   COMMIT;
--
-- Roll back the CODE first. Clean only while no merge or rename has run.
-- Once one has, this is NOT REVERSIBLE without data loss: the kind CHECK
-- re-add fails on 'identity'/'merge' header rows (delete them first, losing
-- the audit), the status CHECK re-add fails on 'merged' rows (decide what
-- they become first -- 'rejected' keeps them unserved), and the old name
-- index fails if a survivor has since taken a merged product's name. The
-- aliases and deal links a merge moved stay moved; product_merge_moves is the
-- only record of where they came from.
