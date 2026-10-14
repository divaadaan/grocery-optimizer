"""Convention checks on ``migrations/*.sql``.

No database. These assert the discipline ``migrations/README.md`` exists to
enforce — numbering, a stated why, transaction wrapping, a rollback block, and
that ``scripts/init_db.sql`` is never edited in place — because the DDL and the
running database have already drifted apart once.

This is deliberately not a parse check. There is no Postgres reachable from the
test environment (no psql, no local server, and the study container is
stopped), so these files have not been executed anywhere; see the report
accompanying this change.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
NAME_RE = re.compile(r"^(\d{3})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")


def migration_files():
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))


MIGRATIONS = migration_files()
IDS = [p.name for p in MIGRATIONS]


def test_migrations_directory_exists():
    assert MIGRATIONS_DIR.is_dir()
    assert (MIGRATIONS_DIR / "README.md").is_file()


def test_there_is_at_least_one_migration():
    assert MIGRATIONS, "expected numbered migrations in migrations/"


@pytest.mark.parametrize("path", MIGRATIONS, ids=IDS)
def test_filename_follows_the_convention(path):
    """``NNN_short_snake_case_description.sql``, three digits, zero padded."""
    assert NAME_RE.match(path.name), path.name


def test_numbers_are_unique():
    numbers = [NAME_RE.match(p.name).group(1) for p in MIGRATIONS]
    assert len(numbers) == len(set(numbers)), f"duplicate migration number in {numbers}"


def test_numbering_starts_at_001_and_has_no_gap_before_the_first():
    numbers = sorted(int(NAME_RE.match(p.name).group(1)) for p in MIGRATIONS)
    assert numbers[0] == 1, "first migration should be 001"


@pytest.mark.parametrize("path", MIGRATIONS, ids=IDS)
def test_header_states_the_why(path):
    head = path.read_text(encoding="utf-8")[:1500]
    assert "-- Why:" in head, f"{path.name} must open with a '-- Why:' comment"


@pytest.mark.parametrize("path", MIGRATIONS, ids=IDS)
def test_header_names_a_design_log_entry(path):
    head = path.read_text(encoding="utf-8")[:1500]
    assert "Design log:" in head, f"{path.name} must name its design-log entry"


@pytest.mark.parametrize("path", MIGRATIONS, ids=IDS)
def test_wrapped_in_a_transaction(path):
    body = path.read_text(encoding="utf-8")
    statements = [
        line.strip() for line in body.splitlines()
        if not line.strip().startswith("--")
    ]
    assert any(s.upper().startswith("BEGIN;") for s in statements), path.name
    assert any(s.upper().startswith("COMMIT;") for s in statements), path.name


@pytest.mark.parametrize("path", MIGRATIONS, ids=IDS)
def test_carries_a_rollback_or_says_why_it_cannot(path):
    body = path.read_text(encoding="utf-8")
    assert "-- Rollback:" in body, f"{path.name} must carry a rollback block"
    tail = body.split("-- Rollback:", 1)[1]
    # Either a real DOWN, or an explicit statement that it is not reversible.
    has_down = "BEGIN;" in tail
    says_irreversible = (
        "not reversible" in tail.lower() or "NOT REVERSIBLE" in tail
    )
    assert has_down or says_irreversible, (
        f"{path.name}: rollback block must contain SQL or say it is not "
        "reversible without data loss"
    )


@pytest.mark.parametrize("path", MIGRATIONS, ids=IDS)
def test_balanced_dollar_quoting(path):
    """Catches a truncated $$...$$ body, the one thing a reader misses easily."""
    body = path.read_text(encoding="utf-8")
    assert body.count("$$") % 2 == 0, f"{path.name} has unbalanced $$ quoting"


@pytest.mark.parametrize("path", MIGRATIONS, ids=IDS)
def test_does_not_edit_the_origin_schema(path):
    """README rule: scripts/init_db.sql is the origin and is never edited.

    A migration that tries to re-create the origin objects is a sign someone
    reached for init_db.sql instead.
    """
    body = path.read_text(encoding="utf-8").upper()
    assert "CREATE TABLE USERS" not in body
    assert "DROP TABLE DEALS" not in body
    assert "DROP TABLE PRICE_SNAPSHOTS" not in body


def test_init_db_sql_still_declares_native_partitioning_not_timescale():
    """scripts/README.md and SETUP.md claim price_snapshots is a TimescaleDB
    hypertable with a retention policy and a price_snapshots_daily continuous
    aggregate. It is not — it is native PG range partitioning, and there is no
    such aggregate. Guards against trusting those docs again."""
    schema = (REPO_ROOT / "scripts" / "init_db.sql").read_text(encoding="utf-8")
    assert "PARTITION BY RANGE (time)" in schema
    assert "create_hypertable" not in schema
    assert "price_snapshots_daily" not in schema
    assert "add_retention_policy" not in schema
    # The only mention of Timescale is the header saying it is NOT required.
    assert 'EXTENSION IF NOT EXISTS "timescaledb"' not in schema


def test_price_snapshots_default_partition_still_exists():
    """The catch-all is what makes current-month inserts work at all, since
    nothing creates the current month's partition (see ../sql4e/DESIGN-LOG.md
    0.5). Ingestion depends on it."""
    schema = (REPO_ROOT / "scripts" / "init_db.sql").read_text(encoding="utf-8")
    assert "price_snapshots_default PARTITION OF price_snapshots DEFAULT" in schema
