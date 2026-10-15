#!/usr/bin/env python
"""Validate and apply numbered migrations to the database in DATABASE_URL.

``make migrate`` in the sibling ``sql4e`` repo applies migrations to the study
database on localhost:5433 and deliberately never touches Neon. This is the
Neon path: the same filename ledger, applied against ``DATABASE_URL``.

Two modes, and ``--check`` is the one to run first::

    python scripts/apply_migrations.py --check     # execute, then ROLL BACK
    python scripts/apply_migrations.py --apply     # execute and commit

``--check`` runs each migration against the real schema inside a transaction it
rolls back, then re-inspects to prove nothing persisted. That catches what a
fake-cursor test cannot — a Postgres syntax error, a constraint that conflicts
with existing rows, a column that already exists — without touching data.

Each migration commits together with its ``schema_migrations`` row, so a
failure leaves neither the DDL nor the ledger entry behind.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

import psycopg
from dotenv import load_dotenv

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(REPO, ".env"))

# A migration file carries its own BEGIN/COMMIT. Strip them so the harness
# controls the transaction — otherwise --check would commit.
_TX_LINE = re.compile(r"^\s*(BEGIN|COMMIT)\s*;\s*$", re.IGNORECASE)


def _body(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return "\n".join(
            line for line in handle.read().splitlines() if not _TX_LINE.match(line)
        )


def _files() -> list[str]:
    return sorted(glob.glob(os.path.join(REPO, "migrations", "*.sql")))


def _url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set (checked the environment and .env).",
              file=sys.stderr)
        raise SystemExit(2)
    return url


def _ledger(url: str) -> set[str]:
    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _counts(url: str) -> dict[str, int]:
    """Row counts we must not change. Compared before and after applying."""
    out: dict[str, int] = {}
    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        for table in ("users", "stores", "deals", "price_snapshots",
                      "recipes", "shopping_lists"):
            cur.execute(f"SELECT count(*) FROM {table}")
            out[table] = cur.fetchone()[0]
    return out


def _tables(url: str) -> set[str]:
    """Public table names, for a before/after leak check."""
    with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s",
            ("public",),
        )
        return {row[0] for row in cur.fetchall()}


def check(url: str, pending: list[str]) -> int:
    """Validate each pending migration in its real context, then roll back.

    Validation is **cumulative**, not per-file-in-isolation: migration N is
    executed on top of 1..N-1 in the same transaction. Migrations legitimately
    depend on their predecessors (007 references the table 006 creates), so
    running one alone against the current schema reports a failure that says
    nothing about the migration. The transaction is rolled back either way, so
    nothing persists.
    """
    before_tables = _tables(url)
    print(f"Validating {len(pending)} migration(s) cumulatively against the live "
          f"schema (rolled back)\n")
    failed = []
    for index, path in enumerate(pending):
        name = os.path.basename(path)
        conn = psycopg.connect(url, autocommit=False)
        try:
            with conn.cursor() as cur:
                # Replay the predecessors so N runs in the schema it expects,
                # then the one under test.
                for prior in pending[:index]:
                    cur.execute(_body(prior))
                cur.execute(_body(path))
            conn.rollback()
            prefix = f"(on 1..{index})" if index else "(on current schema)"
            print(f"  OK       {name}  {prefix}")
        except Exception as exc:  # noqa: BLE001 - reported per migration
            conn.rollback()
            first = str(exc).splitlines()[0]
            print(f"  FAILED   {name}\n             {first}")
            failed.append(name)
        finally:
            conn.close()

    print("\nConfirming nothing persisted")
    # Snapshot-diff rather than naming the tables a migration should create:
    # hardcoded names go stale the moment that migration is applied for real,
    # and then report a false leak on every later run.
    new_tables = sorted(_tables(url) - before_tables)
    leaked = len(new_tables)
    print(f"  tables created by the rolled-back run: {leaked} (expected 0)"
          + (f" -> {new_tables}" if new_tables else ""))

    if failed or leaked:
        print("\nPROBLEMS FOUND")
        return 1
    print("\nALL CLEAN - safe to run with --apply")
    return 0


def apply(url: str, pending: list[str]) -> int:
    before = _counts(url)
    for path in pending:
        name = os.path.basename(path)
        conn = psycopg.connect(url, autocommit=False)
        try:
            with conn.cursor() as cur:
                cur.execute(_body(path))
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (name,)
                )
            conn.commit()
            print(f"  -->  {name}")
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            print(f"  !!!  {name} FAILED, rolled back: {str(exc).splitlines()[0]}",
                  file=sys.stderr)
            return 1
        finally:
            conn.close()

    after = _counts(url)
    print("\nRow counts (must be unchanged - these migrations are additive):")
    drift = False
    for table, count in before.items():
        flag = "" if after[table] == count else "  <-- CHANGED"
        if flag:
            drift = True
        print(f"  {table:18s} {count} -> {after[table]}{flag}")
    if drift:
        print("\nWARNING: row counts changed. Investigate before proceeding.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true",
                       help="execute each migration and roll back; writes nothing")
    group.add_argument("--apply", action="store_true",
                       help="execute and commit, recording each in schema_migrations")
    args = parser.parse_args(argv)

    url = _url()
    host = url.split("@")[-1].split("/")[0]
    print(f"target: {host}\n")

    applied = _ledger(url)
    pending = [p for p in _files() if os.path.basename(p) not in applied]
    if not pending:
        print(f"Nothing pending; {len(applied)} migration(s) already applied.")
        return 0
    for path in _files():
        if os.path.basename(path) in applied:
            print(f"       {os.path.basename(path)} (already applied)")

    return check(url, pending) if args.check else apply(url, pending)


if __name__ == "__main__":
    raise SystemExit(main())
