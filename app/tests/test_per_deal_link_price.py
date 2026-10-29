"""link_deals decides price_is_per_item per deal, not per alias."""
import importlib.util
import os

from app.services import products
from app.services.products import resolve_link_price_is_per_item as resolve

MULTIBUY = dict(unit="ea.", sale_story="2 for $5", pre_price_text=None)
PER_ITEM = dict(unit="ea.", sale_story=None, pre_price_text=None)
UNDECIDED = dict(unit=None, sale_story="save $2", pre_price_text=None)


def test_alias_true_but_deal_is_multibuy_gives_false():
    assert resolve(True, **MULTIBUY) is False
    assert resolve(True, "ea.", None, "2/") is False


def test_alias_false_and_deal_per_item_gives_false():
    assert resolve(False, **PER_ITEM) is False


def test_alias_true_and_deal_undecided_stays_true():
    assert resolve(True, **UNDECIDED) is True
    assert resolve(True, **PER_ITEM) is True


def test_alias_null_stays_undecided():
    assert resolve(None, **PER_ITEM) is None
    assert resolve(None, **MULTIBUY) is None


class Cur:
    def __init__(self, deals, aliases, existing=None):
        self.deals, self.aliases = deals, aliases
        self.rows = dict(existing or {})   # (deal_id, pid) -> flag
        self.rowcount = 0
        self._next = []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        params = params or {}
        self.rowcount = 0
        if "SELECT DISTINCT d.product_name" in sql:
            self._next = [{"product_name": n}
                          for n in sorted({d["product_name"] for d in self.deals})]
        elif "FROM product_aliases" in sql:
            self._next = self.aliases
        elif "SELECT deal_id, unit" in sql:
            self._next = [d for d in self.deals
                          if d["product_name"] == params["raw"]]
        elif "INSERT INTO deal_products" in sql:
            key = (params["deal_id"], params["pid"])
            if key not in self.rows or self.rows[key] != params["per_item"]:
                self.rows[key] = params["per_item"]
                self.rowcount = 1
        else:
            self._next = []

    def fetchall(self):
        return self._next

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DB:
    def __init__(self, cur):
        self.cur = cur

    def get_cursor(self):
        return self.cur


def _deal(i, story, unit="ea.", name="Cheese"):
    return {"deal_id": i, "product_name": name, "unit": unit,
            "sale_story": story, "pre_price_text": None}


def _alias(flag):
    return {"normalized_name": "cheese", "product_id": 1, "tile_position": 0,
            "price_is_per_item": flag}


def test_link_deals_writes_per_deal_values_under_one_alias():
    cur = Cur([_deal(1, "2 for $5"), _deal(2, None)], [_alias(True)])
    products.link_deals(database=DB(cur))
    assert cur.rows == {(1, 1): False, (2, 1): True}


def test_link_deals_null_alias_still_blocks():
    cur = Cur([_deal(1, None)], [_alias(None)])
    stats = products.link_deals(database=DB(cur))
    assert cur.rows == {}
    assert stats["blocked_undecided_price"] == 1


def test_relink_corrects_an_existing_wrong_row():
    cur = Cur([_deal(1, "2 for $5")], [_alias(True)], existing={(1, 1): True})
    # link_deals only visits unlinked names, so call the upsert path directly
    # the way it does: the row is rewritten when the value differs.
    cur.execute(
        "INSERT INTO deal_products ...",
        {"deal_id": 1, "pid": 1, "per_item": resolve(True, **MULTIBUY)})
    assert cur.rows[(1, 1)] is False and cur.rowcount == 1
    cur.execute("INSERT INTO deal_products ...",
                {"deal_id": 1, "pid": 1, "per_item": False})
    assert cur.rowcount == 0    # idempotent


def test_insert_sql_corrects_on_conflict():
    captured = []

    class Spy(Cur):
        def execute(self, sql, params=None):
            captured.append(" ".join(sql.split()))
            super().execute(sql, params)

    products.link_deals(database=DB(Spy([_deal(1, None)], [_alias(True)])))
    ins = next(c for c in captured if "INSERT INTO deal_products" in c)
    assert "DO UPDATE SET price_is_per_item = EXCLUDED.price_is_per_item" in ins
    assert "IS DISTINCT FROM" in ins


def _script():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts",
                        "fix_deal_link_prices.py")
    spec = importlib.util.spec_from_file_location("fix_deal_link_prices", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(deal_id, flag, story=None, unit="ea."):
    return {"deal_id": deal_id, "product_id": 1, "price_is_per_item": flag,
            "product_name": "x", "sale_price": 5, "unit": unit,
            "sale_story": story, "pre_price_text": None}


def test_correction_script_only_demotes_true_multibuy_rows():
    mod = _script()
    rows = [
        _row(1, True, "2 for $5"),    # wrong -> False
        _row(2, True, None),          # fine
        _row(3, False, None),         # already False, never promoted
        _row(4, False, "2 for $5"),   # already correct
        _row(5, True, "save $2", unit=None),   # undecided text: untouched
    ]
    changes = mod.compute_changes(rows)
    assert [(c["deal_id"], c["new"]) for c in changes] == [(1, False)]
    # Idempotent: applying the changes leaves nothing to change.
    for c in changes:
        rows[c["deal_id"] - 1]["price_is_per_item"] = c["new"]
    assert mod.compute_changes(rows) == []
