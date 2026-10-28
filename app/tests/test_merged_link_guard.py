"""Python handling of migration 019's refusal to link a merged product.

No database. The trigger itself is SQL and is validated separately against a
throwaway postgres; here a fake cursor raises an error carrying the trigger's
SQLSTATE, and these tests pin what the callers do with it.
"""
import re

import pytest

from app.services import product_proposer as pp
from app.services import products
from app.services.products import (
    MERGED_LINK_SQLSTATE, is_merged_link_error, merged_survivor_id,
)


class MergedLinkRefused(Exception):
    """Stands in for psycopg's error: .sqlstate and .diag.message_detail."""

    def __init__(self, survivor, sqlstate=MERGED_LINK_SQLSTATE):
        super().__init__("refused: product was merged")
        self.sqlstate = sqlstate

        class _Diag:
            message_detail = f"merged_into={survivor}"

        self.diag = _Diag()


def test_recognised_by_sqlstate_not_message():
    assert is_merged_link_error(MergedLinkRefused(5))
    other = MergedLinkRefused(5, sqlstate="23505")
    assert not is_merged_link_error(other)
    # Right words, wrong code: still not recognised (no text matching).
    assert not is_merged_link_error(RuntimeError("refused: product was merged"))
    assert merged_survivor_id(MergedLinkRefused(5)) == 5
    assert merged_survivor_id(RuntimeError("x")) is None


# ------------------------------------------------------------------ link_deals

class LinkCursor:
    def __init__(self, names, aliases, refuse_pids=()):
        self.names, self.aliases = names, aliases
        self.refuse_pids = set(refuse_pids)
        self.calls = []
        self.rowcount = 0
        self._next = []
        self.written = []          # deal_products inserts that took effect
        self._pending = None       # inserts since the open savepoint

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.calls.append(sql)
        params = params or {}
        if sql.startswith("SAVEPOINT"):
            self._pending = []
        elif sql.startswith("ROLLBACK TO SAVEPOINT"):
            self._pending = []     # discard
        elif sql.startswith("RELEASE SAVEPOINT"):
            self.written += self._pending or []
            self._pending = None
        elif "INSERT INTO deal_products" in sql:
            if params["pid"] in self.refuse_pids:
                raise MergedLinkRefused(survivor=params["pid"] + 100)
            self._pending.append((params["raw"], params["pid"]))
            self.rowcount = 1
        elif "SELECT DISTINCT d.product_name" in sql:
            self._next = [{"product_name": n} for n in self.names]
        elif "FROM product_aliases" in sql:
            self._next = self.aliases
        else:
            self.rowcount = 0
            self._next = []

    def fetchall(self):
        return self._next

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DB:
    def __init__(self, cursor):
        self.cursor = cursor

    def get_cursor(self):
        return self.cursor


def _alias(norm, pid, pos=0):
    return {"normalized_name": norm, "product_id": pid, "tile_position": pos,
            "price_is_per_item": True}


def test_link_deals_unaffected_when_nothing_is_refused():
    cur = LinkCursor(["Milk"], [_alias("milk", 1)])
    stats = products.link_deals(database=DB(cur))
    assert stats["linked_deals"] == 1 and stats["skipped_merged"] == 0
    assert cur.written == [("Milk", 1)]


def test_link_deals_skips_a_refused_name_and_keeps_going():
    cur = LinkCursor(["Cheese", "Milk"],
                     [_alias("cheese", 1), _alias("milk", 2)],
                     refuse_pids={1})
    stats = products.link_deals(database=DB(cur))
    assert stats["skipped_merged"] == 1
    assert stats["linked_deals"] == 1
    assert cur.written == [("Milk", 2)]


def test_a_refused_tile_is_written_whole_or_not_at_all():
    """Product 1 inserts, product 2 is refused: the partial write is rolled
    back so the deal is never gated on a subset of its products."""
    cur = LinkCursor(["Cheese Ham"],
                     [_alias("cheese ham", 1, 1), _alias("cheese ham", 2, 2)],
                     refuse_pids={2})
    stats = products.link_deals(database=DB(cur))
    assert cur.written == []
    assert stats["skipped_merged"] == 1 and stats["linked_deals"] == 0
    assert stats["tile_links"] == 0
    # No legacy-column write for a skipped name.
    assert not any(c.startswith("UPDATE deals") for c in cur.calls)
    assert "ROLLBACK TO SAVEPOINT link_name" in cur.calls


def test_link_deals_never_links_to_the_survivor_from_the_stale_map():
    cur = LinkCursor(["Cheese"], [_alias("cheese", 1)], refuse_pids={1})
    products.link_deals(database=DB(cur))
    assert all(pid != 101 for _, pid in cur.written)


def test_link_deals_reraises_any_other_error():
    class Boom(LinkCursor):
        def execute(self, sql, params=None):
            if "INSERT INTO deal_products" in sql:
                raise MergedLinkRefused(1, sqlstate="23503")
            super().execute(sql, params)

    with pytest.raises(MergedLinkRefused):
        products.link_deals(
            database=DB(Boom(["Milk"], [_alias("milk", 1)])))


# -------------------------------------------------------------------- proposer

PLACEHOLDER = re.compile(r"%\((\w+)\)s")


class ProposerCursor:
    def __init__(self, refuse_pid=None, survivor=None):
        self.refuse_pid, self.survivor = refuse_pid, survivor
        self.calls, self.placed = [], []
        self.rowcount = 0
        self._next = None

    def execute(self, sql, params=None):
        params = params or {}
        self.calls.append(" ".join(sql.split()))
        if "FROM products" in sql and "SELECT" in sql:
            self._next = [{"name": "milk", "product_id": 7, "status": "confirmed"}]
        elif "INSERT INTO product_aliases" in sql and "%(pid)s" in sql:
            if params["pid"] == self.refuse_pid:
                raise MergedLinkRefused(self.survivor)
            self.placed.append(params["pid"])
            self._next = {"times_seen": 1}
        else:
            self._next = None

    def fetchall(self):
        return self._next or []

    def fetchone(self):
        return self._next

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _proposal():
    return pp.Proposal(
        normalized_name="milk", raw_example="MILK", match_existing_product="milk",
        canonical_name="milk", category="dairy",
        attributes={a: False for a in pp.ATTRIBUTES}, is_pantry_staple=False,
        confidence=0.9, reason="r", unmappable=False)


def test_proposer_replaces_a_merged_target_with_the_named_survivor_once():
    cur = ProposerCursor(refuse_pid=7, survivor=9)
    stats = pp.persist_proposals([_proposal()], database=DB(cur))
    assert cur.placed == [9]
    assert stats["aliases_new"] == 1
    assert "ROLLBACK TO SAVEPOINT place_alias" in cur.calls


def test_proposer_does_not_loop_if_the_survivor_is_refused_too():
    cur = ProposerCursor(refuse_pid=7, survivor=7)
    with pytest.raises(MergedLinkRefused):
        pp.persist_proposals([_proposal()], database=DB(cur))
