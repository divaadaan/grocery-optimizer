-- 019_refuse_links_to_merged_products.sql
-- Why: 018 repoints a merged product's aliases and deal links to the survivor,
--   but nothing stops a row being pointed at the merged product afterwards.
--   link_deals reads the alias map, then inserts deal_products rows; a merge
--   that commits between the two leaves a link to a merged product. It fails
--   closed (every reader gates on status = 'confirmed', so the deal is
--   withheld), which is safe but silently wrong. The proposer likewise
--   resolves merged names in Python, with no database backstop.
-- Design log: (new) "product_corrections: rename and merge kinds" (follow-up).
--
-- What changes:
--   refuse_link_to_merged_product()  trigger function: refuses a row whose
--                                    product has status 'merged', naming the
--                                    survivor (merged_into).
--   trg_deal_products_refuse_merged  BEFORE INSERT OR UPDATE OF product_id
--   trg_product_aliases_refuse_merged  on deal_products / product_aliases.
--
-- The refusal carries SQLSTATE 'GO001' and DETAIL 'merged_into=<id>' so the
--   caller can recognise it without matching message text, and re-resolve or
--   skip. Must apply after 018 (status 'merged' and merged_into). Existing
--   rows are not examined: a link that already points at a merged product
--   stays (withheld) until a merge's own repoint or a reviewer moves it.
--   Not applied anywhere by this change.
--
-- Locking. The trigger reads the product row with FOR SHARE. A merge takes
--   FOR UPDATE on both products before it repoints anything, so the two
--   serialise on the product row either way round:
--     * the link's transaction holds the share lock first: the merge blocks
--       until the link commits, then repoints it (it sees the committed row).
--     * the merge holds the lock first: the link's SELECT ... FOR SHARE blocks
--       until the merge commits, then (READ COMMITTED re-reads the locked row)
--       sees status = 'merged' and refuses.
--   A plain SELECT would not block, and could read 'confirmed' just before the
--   merge commits, which is exactly the race. FOR SHARE (not KEY SHARE) is
--   deliberate: KEY SHARE does not conflict with the non-key UPDATE that sets
--   status = 'merged', so it would only be safe because the app also happens
--   to take FOR UPDATE first. This does not lean on that.
--   Only the one product row is locked, only for the length of the link's
--   transaction, and ordinary links (many sharing one product) do not conflict
--   with each other.
--
-- Not fired when a merge repoints rows to the survivor: the survivor is
--   confirmed. Not fired by a status-only update of an alias (the trigger is
--   UPDATE OF product_id).

BEGIN;

CREATE OR REPLACE FUNCTION refuse_link_to_merged_product() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    p_status TEXT;
    p_into   INTEGER;
BEGIN
    IF NEW.product_id IS NULL THEN
        RETURN NEW;     -- an unplaced alias names no product
    END IF;

    SELECT status, merged_into INTO p_status, p_into
    FROM products
    WHERE product_id = NEW.product_id
    FOR SHARE;          -- see "Locking" in the header

    IF p_status = 'merged' THEN
        RAISE EXCEPTION 'refused: product % was merged into %; link % instead',
            NEW.product_id, p_into, p_into
            USING ERRCODE = 'GO001',
                  DETAIL  = 'merged_into=' || p_into,
                  HINT    = 'Re-resolve the product to its survivor and retry.';
    END IF;
    RETURN NEW;
    -- A missing product falls through: the foreign key reports it.
END;
$$;

DROP TRIGGER IF EXISTS trg_deal_products_refuse_merged ON deal_products;
CREATE TRIGGER trg_deal_products_refuse_merged
    BEFORE INSERT OR UPDATE OF product_id ON deal_products
    FOR EACH ROW EXECUTE FUNCTION refuse_link_to_merged_product();

DROP TRIGGER IF EXISTS trg_product_aliases_refuse_merged ON product_aliases;
CREATE TRIGGER trg_product_aliases_refuse_merged
    BEFORE INSERT OR UPDATE OF product_id ON product_aliases
    FOR EACH ROW EXECUTE FUNCTION refuse_link_to_merged_product();

COMMENT ON FUNCTION refuse_link_to_merged_product() IS
    'Refuses a deal_products / product_aliases row whose product is merged (SQLSTATE GO001, DETAIL merged_into=<survivor>). Takes FOR SHARE on the product so a concurrent merge serialises against the link.';

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP TRIGGER IF EXISTS trg_product_aliases_refuse_merged ON product_aliases;
--   DROP TRIGGER IF EXISTS trg_deal_products_refuse_merged ON deal_products;
--   DROP FUNCTION IF EXISTS refuse_link_to_merged_product();
--   COMMIT;
--
-- Clean at any time: it adds no data and rewrites nothing. Roll back the CODE
-- first only if it matters: without the trigger the Python handling is inert,
-- not wrong, and the race returns to being fail-closed and silent.
