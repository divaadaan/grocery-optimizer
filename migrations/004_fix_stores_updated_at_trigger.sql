-- 004_fix_stores_updated_at_trigger.sql
-- Why: every UPDATE on stores currently raises. init_db.sql attaches the shared
--   update_updated_at_column() trigger to stores, but that function assigns
--   NEW.updated_at and stores has no such column - it has last_updated. So
--   `UPDATE stores SET ...` fails with:
--       ERROR 42703: record "new" has no field "updated_at"
--   Found while writing the ingestion store upsert. Nothing in the app had ever
--   updated a store row, so the bug had never fired.
-- Design log: (new) "stores has last_updated, not updated_at, and its trigger
--   does not know".
-- Hernandez: field-level integrity - a field's name is part of its definition.
--   Two names for the same fact across sibling tables is the drift this
--   migrations directory exists to catch.

BEGIN;

-- A stores-specific trigger function rather than renaming the column: the column
-- name last_updated is already in the SELECT list of
-- app/services/store_service.py::get_stores_by_postal_code, and renaming it would
-- be an application-visible change for no gain. Additive-first (migrations
-- README rule 3): the shared function is left exactly as it is for users/deals.
CREATE OR REPLACE FUNCTION update_stores_last_updated_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.last_updated = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_stores_updated_at ON stores;

CREATE TRIGGER update_stores_last_updated
    BEFORE UPDATE ON stores
    FOR EACH ROW EXECUTE FUNCTION update_stores_last_updated_column();

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP TRIGGER IF EXISTS update_stores_last_updated ON stores;
--   DROP FUNCTION IF EXISTS update_stores_last_updated_column();
--   CREATE TRIGGER update_stores_updated_at BEFORE UPDATE ON stores
--       FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
--   COMMIT;
--
-- Fully reversible. Note that the rollback restores a trigger that makes every
-- UPDATE on stores fail, which is the origin-schema behaviour.
