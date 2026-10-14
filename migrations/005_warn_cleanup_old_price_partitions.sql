-- 005_warn_cleanup_old_price_partitions.sql
-- Why: price_snapshots is now the basis of a self-derived price baseline
--   (app/services/price_baseline.py), which needs 4-8 WEEKS of history before it
--   returns anything at all. cleanup_old_price_partitions() drops partitions
--   older than 90 days. Today that is harmless by accident; the moment anyone
--   lands the Unit 9 partition fix it becomes a baseline-destroying job. This
--   migration attaches that warning to the function itself, where a DBA reading
--   \df+ will see it, rather than leaving it only in a design log.
-- Design log: 0.6 "cleanup_old_price_partitions() is safe, but by accident", and
--   0.5 "The partition bug is not the one the plan describes".
-- Hernandez: not a structural rule - this is documentation-as-schema. The
--   relevant discipline is that a destructive routine must state what it
--   destroys.
--
-- No DDL beyond COMMENT: deliberately NOT rewriting or dropping the function.
-- Design log 0.6 schedules that rewrite (pg_partition_tree + real partition
-- bounds + a narrowed exception handler) for Unit 9, and one concern per
-- migration means this file does not pre-empt it.

BEGIN;

COMMENT ON FUNCTION cleanup_old_price_partitions() IS
$$DESTRUCTIVE. DO NOT SCHEDULE. Drops price_snapshots partitions whose name
parses to a month older than CURRENT_DATE - 90 days.

Why it must not be scheduled: app/services/price_baseline.py derives a trailing
median advertised price from price_snapshots over a 56-day window and refuses to
return a baseline until it has 4+ distinct weeks of observations. A 90-day
retention cutoff leaves barely more history than the baseline window needs, so
running this on a schedule caps the project's ability to ever measure its own
price history.

Why it is currently harmless, and why that is luck rather than design (see
../sql4e/DESIGN-LOG.md 0.5 and 0.6):
  1. It iterates pg_tables LIKE 'price_snapshots_%', which matches
     price_snapshots_default, and feeds the suffix to TO_DATE(..,'YYYY_MM').
     TO_DATE('default','YYYY_MM') raises, and the loop's bare
     EXCEPTION WHEN OTHERS swallows it into a NOTICE. So the default partition
     is skipped.
  2. create_price_snapshot_partition() only ever creates NEXT month and nothing
     creates the current one, so in practice ALL rows live in
     price_snapshots_default. The only droppable partitions are the empty
     2025_09..2025_12 ones from init_db.sql.

Both of those are accidents. Landing the Unit 9 partition fix - which moves real
data into real monthly partitions - converts this function from a no-op into a
job that silently deletes the price baseline. Fix the function in the same
migration that fixes the partitions, or not at all.$$;

COMMENT ON FUNCTION create_price_snapshot_partition() IS
$$Creates NEXT month's price_snapshots partition only; nothing creates the
current month, so rows for any month this was not run ahead of land in
price_snapshots_default permanently. Backfilling the missed partition afterwards
fails with ERROR 23514 (default partition would be violated). See
../sql4e/DESIGN-LOG.md 0.5; proper fix (DETACH / relocate / CREATE / ATTACH)
scheduled for Unit 9.$$;

COMMIT;

-- Rollback:
--   BEGIN;
--   COMMENT ON FUNCTION cleanup_old_price_partitions() IS NULL;
--   COMMENT ON FUNCTION create_price_snapshot_partition() IS NULL;
--   COMMIT;
--
-- Fully reversible; comments only, no structural change.
