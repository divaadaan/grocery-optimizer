-- 020_ingestion_runs.sql
-- Why: ingestion produces an IngestionReport that is only logged, never kept.
--   Nothing records run history, freshness, or parse-failure rates, so a week
--   where Flipp silently returned nothing looks identical to a week where it
--   worked. One row per real ingestion run makes "when did we last get good
--   data, and how good was it" a query instead of a log grep.
-- Design log: (new) "ingestion_runs: run history and a health verdict".
-- Hernandez: one fact per row -- a run is one event; its outcome counters are
--   facts about that event, so they live on its row, not in a side table.
--
-- Shape: key columns PLUS a JSONB `counters`. The key columns are the ones the
--   health rule, the freshness check and any dashboard filter or sort on, so
--   they are real, typed, CHECKed columns. `counters` holds the whole
--   IngestionReport.as_dict() (every skip counter, duplicate counts, the names
--   of the failed terms), so adding a counter later is a code change, not a
--   migration, and no run is ever recorded with less than the report knew.
--   One column per counter was rejected: 20 mostly-unqueried columns that need
--   a migration each time the report grows.
--
-- What changes:
--   ingestion_runs  one row per real (non-dry-run) ingestion, written by every
--                   entry point (CLI, refresh route, cold-start auto-ingest).
--                   Written in its own transaction AFTER the ingestion's, so a
--                   failure to write the row can never fail or roll back the
--                   ingestion. No code path deletes from it.
--
-- Must apply after 001 (references nothing, but the app reads stores/deals for
--   the live-deal count). Independent of 015-019. Not applied anywhere by this
--   change. The app tolerates the table being absent: a failed run-row write is
--   logged and swallowed, so code from this branch can run before 020 is applied.

BEGIN;

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id              BIGSERIAL PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL,
    finished_at         TIMESTAMPTZ NOT NULL,
    trigger             VARCHAR(16) NOT NULL,
    postal_code         VARCHAR(10) NOT NULL,
    source              VARCHAR(32) NOT NULL,
    -- Dry runs are not recorded today (they write nothing by contract), but the
    -- flag stays so a future "record dry runs too" needs no migration.
    dry_run             BOOLEAN NOT NULL DEFAULT false,

    status              VARCHAR(12) NOT NULL,
    -- why a run is 'unhealthy' (assess_health), one short string per rule hit
    health_reasons      TEXT[] NOT NULL DEFAULT '{}',
    -- exception text when status = 'failed'
    error               TEXT,

    -- health fields: the numbers the verdict is computed from
    terms_searched      INTEGER NOT NULL DEFAULT 0,
    terms_failed        INTEGER NOT NULL DEFAULT 0,
    raw_items           INTEGER NOT NULL DEFAULT 0,
    normalized          INTEGER NOT NULL DEFAULT 0,
    skipped_total       INTEGER NOT NULL DEFAULT 0,
    deals_inserted      INTEGER NOT NULL DEFAULT 0,
    deals_updated       INTEGER NOT NULL DEFAULT 0,
    snapshots_inserted  INTEGER NOT NULL DEFAULT 0,
    -- fraction of normalised deals carrying a pre-promo price / lacking a unit
    regular_price_coverage NUMERIC(4,3),
    unit_null_rate      NUMERIC(4,3),

    -- the full IngestionReport.as_dict()
    counters            JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT chk_ingestion_runs_trigger
        CHECK (trigger IN ('cli', 'refresh_route', 'cold_start')),
    CONSTRAINT chk_ingestion_runs_status
        CHECK (status IN ('ok', 'unhealthy', 'failed')),
    CONSTRAINT chk_ingestion_runs_failed_has_error
        CHECK (status <> 'failed' OR error IS NOT NULL),
    CONSTRAINT chk_ingestion_runs_times
        CHECK (finished_at >= started_at),
    CONSTRAINT chk_ingestion_runs_counts
        CHECK (terms_searched >= 0 AND terms_failed >= 0 AND raw_items >= 0
               AND normalized >= 0 AND skipped_total >= 0
               AND deals_inserted >= 0 AND deals_updated >= 0
               AND snapshots_inserted >= 0),
    CONSTRAINT chk_ingestion_runs_rates
        CHECK ((regular_price_coverage IS NULL OR regular_price_coverage BETWEEN 0 AND 1)
               AND (unit_null_rate IS NULL OR unit_null_rate BETWEEN 0 AND 1))
);

-- "latest run for this postal code", the freshness check's only access path.
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_postal_finished
    ON ingestion_runs (postal_code, finished_at DESC);

-- "latest GOOD run" skips failed and unhealthy runs without scanning them.
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_ok_postal_finished
    ON ingestion_runs (postal_code, finished_at DESC)
    WHERE status = 'ok' AND NOT dry_run;

COMMENT ON TABLE ingestion_runs IS
    'One row per real ingestion run (CLI, refresh route, cold-start). Written in a separate transaction after the ingestion, so a failed write never fails the run. status: ok | unhealthy (assess_health rules, see health_reasons) | failed (an exception, see error). counters is the full IngestionReport.';
COMMENT ON COLUMN ingestion_runs.trigger IS
    'Which entry point ran it: cli (scripts/ingest_deals.py), refresh_route (POST /postal-code/refresh or discover?refresh=true), cold_start (discover auto-ingest on an empty postal code).';
COMMENT ON COLUMN ingestion_runs.skipped_total IS
    'Raw items dropped by normalisation (no name, no price, non-food, bad dates, no merchant, no item id). Duplicates collapsed by dedupe are NOT skips.';

COMMIT;

-- Rollback:
--   BEGIN;
--   DROP TABLE IF EXISTS ingestion_runs;
--   COMMIT;
--
-- Roll back the CODE first (it writes this table, though it swallows a missing
-- table). The drop loses run history only; nothing else references the table.
-- Not reversible without data loss once runs have been recorded.
