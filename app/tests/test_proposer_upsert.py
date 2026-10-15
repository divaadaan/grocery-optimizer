"""The alias upsert, executed for real rather than against a recording fake.

SQLite understands the same ``ON CONFLICT (...) WHERE ... DO UPDATE`` syntax
(partial-index conflict targets, ``excluded``, ``RETURNING``), so the very SQL
text the importer sends to Postgres is run here with only the placeholder
style translated.
"""
import re
import sqlite3

import pytest

from app.services import product_proposer as pp

_SCHEMA = """
CREATE TABLE product_aliases (
    alias_id INTEGER PRIMARY KEY, normalized_name TEXT, raw_example TEXT,
    product_id INTEGER, status TEXT, source TEXT, confidence REAL,
    proposal_reason TEXT, tile_position INTEGER, price_is_per_item INTEGER,
    times_seen INTEGER DEFAULT 1);
CREATE UNIQUE INDEX u_placed ON product_aliases (normalized_name, product_id)
    WHERE product_id IS NOT NULL;
CREATE UNIQUE INDEX u_unplaced ON product_aliases (normalized_name)
    WHERE product_id IS NULL;
"""


def _conn(product_id, status, existing):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO product_aliases (normalized_name, raw_example, product_id,"
        " status, source, confidence, proposal_reason, tile_position,"
        " price_is_per_item) VALUES ('milk', 'MILK', ?, ?, 'x', 0.9, 'orig', 0, ?)",
        (product_id, status, existing),
    )
    return conn


def _upsert(conn, sql_text, product_id, incoming):
    sql = re.sub(r"%\((\w+)\)s", r":\1", sql_text)
    params = {"norm": "milk", "raw": "MILK 2L", "by": "y", "conf": 0.1,
              "reason": "new", "pos": 3, "per_item": incoming}
    if ":pid" in sql:
        params["pid"] = product_id
    conn.execute(sql, params)
    return conn.execute("SELECT * FROM product_aliases").fetchone()


CASES = [
    ("confirmed", None, True, 1),    # fills NULL on a confirmed alias
    ("proposed", None, False, 0),    # False is a value, and fills NULL too
    ("confirmed", 1, False, 1),      # never overwrites a decision
    ("confirmed", 0, True, 0),
    ("proposed", None, None, None),  # nothing to fill with
]


@pytest.mark.parametrize("status, existing, incoming, expected", CASES)
@pytest.mark.parametrize("placed", [True, False])
def test_upsert_fills_null_pricing_only(placed, status, existing, incoming, expected):
    pid = 7 if placed else None
    sql = pp._ALIAS_UPSERT_PLACED if placed else pp._ALIAS_UPSERT_UNPLACED
    row = _upsert(_conn(pid, status, existing), sql, pid, incoming)

    assert row["price_is_per_item"] == expected
    assert row["times_seen"] == 2
    # Review state and attributes are untouched by a re-import.
    assert row["status"] == status
    assert row["confidence"] == 0.9
    assert row["proposal_reason"] == "orig"
    assert row["tile_position"] == 0
    assert row["raw_example"] == "MILK"
