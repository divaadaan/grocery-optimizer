"""Audited renames and merges of curated products (migration 018).

No database, network or LLM. ``FakeDB`` is a small in-memory model of the
tables these services touch (products, product_aliases, deal_products, deals,
price_snapshots, users and the audit tables). It answers each scripted
statement from that state and applies writes to it; a ``get_cursor`` block
that raises restores the state it started with, the way the real pool rolls
back -- so "atomic", "rolled back" and "dry run writes nothing" are
observable as state, not just as call lists. An unscripted statement fails
the test rather than being ignored.

The HTTP layer is driven through ``httpx.ASGITransport`` (see CLAUDE.md) with
the real services bound to the fake.
"""
import copy
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import partial

import httpx
import psycopg.errors
import pytest

from app.agents import dietary
from app.agents.sous_chef import offered_staples
from app.main import app
from app.services import product_identity, products

A, S, OTHER = 20, 10, 30          # absorbed, survivor, an unrelated product


def _attrs(**overrides):
    row = {a: False for a in products.ATTRIBUTES}
    row.update(overrides)
    return row


def _product(pid, name, status="confirmed", category="dairy", **attrs):
    return {"product_id": pid, "canonical_name": name, "category": category,
            "status": status, "merged_into": None, "is_pantry_staple": False,
            "assumed_on_hand": False, **_attrs(**attrs)}


def _state(absorbed=None, survivor=None):
    """``absorbed`` / ``survivor``: overrides on A and S, which both start
    confirmed with contains_dairy=True and every other attribute False."""
    return {
        "products": {
            A: {**_product(A, "cheddar", contains_dairy=True), **(absorbed or {})},
            S: {**_product(S, "cheddar cheese", contains_dairy=True),
                **(survivor or {})},
            OTHER: _product(OTHER, "crackers", category="pantry"),
        },
        "aliases": {
            1: _alias(1, "cheddar", A, "confirmed"),          # repoint
            2: _alias(2, "cheddar block", A, "confirmed"),    # twin on S -> retire
            3: _alias(3, "cheddar block", S, "confirmed"),
            4: _alias(4, "old cheddar", A, "proposed"),       # repoint
            5: _alias(5, "cheddar bar", A, "rejected"),       # repoint (inert)
        },
        # (deal_id, product_id) -> junction row
        "deal_products": {
            (100, A): {"tile_position": 0, "price_is_per_item": True},
            (101, S): {"tile_position": 0, "price_is_per_item": True},
            (101, A): {"tile_position": 1, "price_is_per_item": False},
            (102, OTHER): {"tile_position": 0, "price_is_per_item": True},
            (102, A): {"tile_position": 1, "price_is_per_item": True},
            (103, S): {"tile_position": 0, "price_is_per_item": True},
        },
        "deals": {
            100: _deal(100, product_id=A),
            101: _deal(101),
            102: _deal(102),
            103: _deal(103, product_id=S),
            104: _deal(104, live=False, product_id=A),   # legacy column only
        },
        "snapshots": [A, A, S],
        "users": [
            {"user_id": 1, "dietary_restrictions": ["vegan"]},
            {"user_id": 2, "dietary_restrictions": ["vegetarian"]},
            {"user_id": 3, "dietary_restrictions": ["no_nuts"]},
            {"user_id": 4, "dietary_restrictions": []},
        ],
        "product_corrections": [],
        "product_identity_corrections": [],
        "product_merge_corrections": [],
        "product_merge_moves": [],
    }


def _alias(alias_id, name, pid, status):
    return {"alias_id": alias_id, "normalized_name": name, "product_id": pid,
            "status": status, "tile_position": 0, "price_is_per_item": True,
            "reviewed_by": None}


def _deal(deal_id, live=True, product_id=None):
    return {"deal_id": deal_id, "product_name": f"tile {deal_id}", "store_id": 1,
            "valid_from": date(2026, 10, 20),
            "valid_until": date(2026, 11, 3) if live else date(2026, 10, 1),
            "live": live, "product_id": product_id}


class FakeCursor:
    def __init__(self, fake):
        self.fake = fake
        self._one = None
        self._all = []
        self.rowcount = 0

    def _ret(self, rows):
        self._all = rows
        self._one = rows[0] if rows else None
        self.rowcount = len(rows)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        p = params or {}
        st = self.fake.state
        self.fake.calls.append((s, params))
        for prefix in self.fake.fail_on:
            if s.startswith(prefix):
                raise RuntimeError(f"injected failure on {prefix}")
        if self.fake.unique_violation_on and s.startswith(self.fake.unique_violation_on):
            raise psycopg.errors.UniqueViolation("duplicate key value")

        if s.startswith("SELECT product_id, canonical_name, category, status, merged_into"):
            row = st["products"].get(p["pid"])
            if "FOR UPDATE" in s:
                self.fake.locked.append(p["pid"])
            self._ret([dict(row)] if row else [])
        elif s.startswith("SELECT product_id, canonical_name, status FROM products "
                          "WHERE lower(canonical_name)"):
            self._ret([
                {k: r[k] for k in ("product_id", "canonical_name", "status")}
                for r in st["products"].values()
                if r["canonical_name"].lower() == p["name"].lower()
                and r["status"] in ("proposed", "confirmed")
                and r["product_id"] != p["pid"]])
        elif s.startswith("SELECT product_id FROM products WHERE merged_into"):
            self._ret([{"product_id": r["product_id"]}
                       for r in st["products"].values()
                       if r["merged_into"] == p["pid"]])
        elif s.startswith("SELECT alias_id, normalized_name, status, tile_position"):
            self._ret([dict(a) for a in sorted(st["aliases"].values(),
                                               key=lambda a: a["alias_id"])
                       if a["product_id"] == p["pid"]])
        elif s.startswith("SELECT alias_id, normalized_name, status FROM product_aliases"):
            self._ret([dict(a) for a in st["aliases"].values()
                       if a["product_id"] == p["pid"]
                       and a["normalized_name"] in p["names"]])
        elif s.startswith("SELECT dp.deal_id, dp.tile_position"):
            self._ret([
                {"deal_id": d, **row,
                 "survivor_linked": (d, p["sid"]) in st["deal_products"]}
                for (d, pid), row in sorted(st["deal_products"].items())
                if pid == p["aid"]])
        elif s.startswith("SELECT deal_id FROM deals WHERE product_id"):
            self._ret([{"deal_id": d["deal_id"]} for d in st["deals"].values()
                       if d["product_id"] == p["aid"]])
        elif s.startswith("SELECT count(*) AS n FROM price_snapshots"):
            self._ret([{"n": st["snapshots"].count(p["aid"])}])
        elif "FROM deal_products target" in s:
            self._ret(self.fake.deal_rows(p["pid"]))
        elif "FROM users" in s:
            self._ret([dict(u) for u in st["users"]])

        elif s.startswith("INSERT INTO product_corrections"):
            cid = 500 + len(st["product_corrections"])
            st["product_corrections"].append({"correction_id": cid, **p})
            self._ret([{"correction_id": cid,
                        "corrected_at": datetime(2026, 10, 28, tzinfo=timezone.utc)}])
        elif s.startswith("INSERT INTO product_identity_corrections"):
            st["product_identity_corrections"].append(dict(p))
        elif s.startswith("INSERT INTO product_merge_corrections"):
            st["product_merge_corrections"].append(dict(p))
        elif s.startswith("INSERT INTO product_merge_moves"):
            st["product_merge_moves"].append(
                {**p, "detail": p["detail"].obj if p["detail"] is not None else None})

        elif s.startswith("UPDATE products SET status = 'merged'"):
            row = st["products"][p["aid"]]
            if row["status"] == "confirmed":
                row.update(status="merged", merged_into=p["sid"])
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif s.startswith("UPDATE products SET"):                 # rename
            row = st["products"][p["pid"]]
            fields = [f for f in ("canonical_name", "category") if f in p]
            assert set(p) == set(fields) | {"pid", "status"}, p
            if row["status"] == p["status"]:
                row.update({f: p[f] for f in fields})
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif s.startswith("UPDATE price_snapshots"):
            n = st["snapshots"].count(p["aid"])
            st["snapshots"] = [p["sid"] if x == p["aid"] else x for x in st["snapshots"]]
            self.rowcount = n
        elif s.startswith("UPDATE product_aliases SET product_id"):
            out = []
            for i in p["ids"]:
                a = st["aliases"][i]
                if a["product_id"] == p["aid"]:
                    if any(b["normalized_name"] == a["normalized_name"]
                           and b["product_id"] == p["sid"]
                           for b in st["aliases"].values()):
                        raise psycopg.errors.UniqueViolation("uq_product_aliases_name_product")
                    a["product_id"] = p["sid"]
                    out.append({"alias_id": i})
            self._ret(out)
        elif s.startswith("UPDATE product_aliases SET status = 'rejected'"):
            out = []
            for i in p["ids"]:
                a = st["aliases"][i]
                if a["product_id"] == p["aid"] and a["status"] != "rejected":
                    a.update(status="rejected", reviewed_by=p["who"])
                    out.append({"alias_id": i})
            self._ret(out)
        elif s.startswith("UPDATE deal_products SET product_id"):
            out = []
            for d in p["ids"]:
                if (d, p["sid"]) in st["deal_products"]:
                    raise psycopg.errors.UniqueViolation("deal_products_pkey")
                row = st["deal_products"].pop((d, p["aid"]), None)
                if row is not None:
                    st["deal_products"][(d, p["sid"])] = row
                    out.append({"deal_id": d})
            self._ret(out)
        elif s.startswith("DELETE FROM deal_products"):
            out = [{"deal_id": d} for d in p["ids"]
                   if st["deal_products"].pop((d, p["aid"]), None) is not None]
            self._ret(out)
        elif s.startswith("UPDATE deals SET product_id"):
            out = []
            for d in st["deals"].values():
                if d["product_id"] == p["aid"]:
                    d["product_id"] = p["sid"]
                    out.append({"deal_id": d["deal_id"]})
            self._ret(out)
        else:  # pragma: no cover - a new query should be scripted, not ignored
            raise AssertionError(f"unscripted SQL: {s[:90]}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeDB:
    def __init__(self, state=None):
        self.state = state or _state()
        self.calls = []
        self.locked = []
        self.fail_on = []
        self.unique_violation_on = None
        self.transactions = 0
        self.rolled_back = 0

    @contextmanager
    def get_cursor(self):
        self.transactions += 1
        snapshot = copy.deepcopy(self.state)
        try:
            yield FakeCursor(self)
        except BaseException:
            self.state = snapshot
            self.rolled_back += 1
            raise

    def deal_rows(self, pid):
        st = self.state
        out = []
        for deal_id in sorted({d for d, p in st["deal_products"] if p == pid}):
            deal = st["deals"][deal_id]
            linked = sorted(((row["tile_position"], p) for (d, p), row
                             in st["deal_products"].items() if d == deal_id))
            for _, linked_pid in linked:
                prod = st["products"][linked_pid]
                out.append({
                    **{k: deal[k] for k in ("deal_id", "product_name", "store_id",
                                            "valid_from", "valid_until", "live")},
                    "linked_product_id": linked_pid,
                    "linked_status": prod["status"],
                    **{a: prod[a] for a in products.ATTRIBUTES}})
        return out

    def linked(self, deal_id):
        """deal_allows input for one deal, read from the current state."""
        st = self.state
        return [{"product_id": p, "status": st["products"][p]["status"],
                 **{a: st["products"][p][a] for a in products.ATTRIBUTES}}
                for (d, p) in sorted(st["deal_products"]) if d == deal_id]

    def writes(self):
        return [s for s, _ in self.calls
                if s.startswith(("INSERT", "UPDATE", "DELETE"))]


def _merge(fake, absorbed=A, survivor=S, dry_run=False, reviewer="divad",
           reason="same block of cheddar, two names"):
    return product_identity.merge_products(
        absorbed, survivor, reviewer, reason, dry_run=dry_run, database=fake)


def _rename(fake, pid=A, name=None, category=None, dry_run=False,
            reviewer="divad", reason="label says fish paste"):
    return product_identity.rename_product(
        pid, name, category, reviewer, reason, dry_run=dry_run, database=fake)


# ==========================================================================
# Rename / recategorise
# ==========================================================================

def _p84_state():
    st = _state()
    st["products"][84] = _product(84, "cheese tofu", category="deli",
                                  contains_dairy=True)
    return st


def test_rename_happy_path_writes_audit_and_only_identity_columns():
    fake = FakeDB(_p84_state())
    before_attrs = {a: fake.state["products"][84][a] for a in products.ATTRIBUTES}
    out = _rename(fake, 84, "fish tofu", "seafood")

    assert fake.transactions == 1 and fake.rolled_back == 0
    [header] = fake.state["product_corrections"]
    assert header["pid"] == 84 and header["kind"] == "identity"
    assert header["who"] == "divad" and header["why"] == "label says fish paste"
    assert fake.state["product_identity_corrections"] == [
        {"cid": 500, "field": "canonical_name",
         "old_value": "cheese tofu", "new_value": "fish tofu"},
        {"cid": 500, "field": "category", "old_value": "deli", "new_value": "seafood"},
    ]
    row = fake.state["products"][84]
    assert (row["canonical_name"], row["category"]) == ("fish tofu", "seafood")

    # Attributes untouched: the state, and the UPDATE statement itself.
    assert {a: row[a] for a in products.ATTRIBUTES} == before_attrs
    [update] = [s for s in fake.writes() if s.startswith("UPDATE products")]
    assert not any(a in update for a in products.ATTRIBUTES)
    assert out["attributes_changed"] is False
    assert out["product"]["attributes"] == before_attrs

    # The response points at the separate attribute correction.
    assert out["attribute_review"]["endpoint"] == \
        "/admin/products/84/attribute-corrections"
    assert out["correction_id"] == 500 and out["dry_run"] is False
    assert out["product"]["before"] == {"canonical_name": "cheese tofu",
                                        "category": "deli"}
    assert out["product"]["after"] == {"canonical_name": "fish tofu",
                                       "category": "seafood"}
    assert fake.locked == [84]


def test_rename_name_only_records_one_field_and_normalises_whitespace():
    fake = FakeDB(_p84_state())
    _rename(fake, 84, "  fish   paste tofu ")
    assert [r["field"] for r in fake.state["product_identity_corrections"]] == [
        "canonical_name"]
    assert fake.state["products"][84]["canonical_name"] == "fish paste tofu"
    assert fake.state["products"][84]["category"] == "deli"


def test_rename_category_is_lowercased():
    fake = FakeDB(_p84_state())
    _rename(fake, 84, category=" Sea Food ")
    assert fake.state["products"][84]["category"] == "sea food"


def test_rename_dry_run_writes_nothing_and_takes_no_lock():
    fake = FakeDB(_p84_state())
    before = copy.deepcopy(fake.state)
    out = _rename(fake, 84, "fish tofu", dry_run=True)
    assert fake.state == before and fake.writes() == [] and fake.locked == []
    assert out["dry_run"] is True and out["correction_id"] is None
    assert out["changes"] == [{"field": "canonical_name",
                               "old_value": "cheese tofu", "new_value": "fish tofu"}]


def test_rename_collision_with_a_confirmed_product_suggests_a_merge():
    fake = FakeDB(_p84_state())
    with pytest.raises(product_identity.NameCollision) as err:
        _rename(fake, 84, "Cheddar Cheese")           # case-insensitive
    assert err.value.other["product_id"] == S
    assert "/admin/products/84/merge" in str(err.value)
    assert fake.state["product_corrections"] == [] and fake.writes() == []


def test_rename_collision_with_a_proposed_product_points_at_review():
    st = _p84_state()
    st["products"][S]["status"] = "proposed"
    with pytest.raises(product_identity.NameCollision, match="/admin/review"):
        _rename(FakeDB(st), 84, "cheddar cheese")


def test_rename_ignores_rejected_and_merged_name_holders_and_itself():
    st = _p84_state()
    st["products"][S]["status"] = "rejected"
    st["products"][A].update(status="merged", merged_into=OTHER)
    fake = FakeDB(st)
    _rename(fake, 84, "cheddar cheese")
    _rename(fake, 84, "cheddar")
    _rename(fake, 84, "CHEDDAR")                       # case-only: itself
    assert fake.state["products"][84]["canonical_name"] == "CHEDDAR"


def test_rename_race_on_the_unique_index_is_a_refusal_and_rolls_back():
    fake = FakeDB(_p84_state())
    fake.unique_violation_on = "UPDATE products SET"
    with pytest.raises(product_identity.CorrectionRefused, match="taken"):
        _rename(fake, 84, "fish tofu")
    assert fake.rolled_back == 1
    assert fake.state["product_corrections"] == []
    assert fake.state["products"][84]["canonical_name"] == "cheese tofu"


def test_rename_failure_rolls_back_the_audit_rows():
    fake = FakeDB(_p84_state())
    fake.fail_on = ["UPDATE products SET"]
    with pytest.raises(RuntimeError):
        _rename(fake, 84, "fish tofu")
    assert fake.rolled_back == 1
    assert fake.state["product_corrections"] == []
    assert fake.state["product_identity_corrections"] == []


@pytest.mark.parametrize("name, category, message", [
    (None, None, "canonical_name and/or a category"),
    ("   ", None, "blank"),
    (None, "", "blank"),
    (5, None, "must be a string"),
    ("x" * 121, None, "longer than 120"),
])
def test_rename_invalid_requests(name, category, message):
    fake = FakeDB(_p84_state())
    with pytest.raises(product_identity.CorrectionInvalid, match=message):
        _rename(fake, 84, name, category)
    assert fake.calls == []


@pytest.mark.parametrize("reviewer, reason", [("", "r"), ("divad", " "), (None, "r")])
def test_rename_requires_reviewer_and_reason(reviewer, reason):
    fake = FakeDB(_p84_state())
    with pytest.raises(product_identity.CorrectionInvalid, match="required"):
        _rename(fake, 84, "fish tofu", reviewer=reviewer, reason=reason)
    assert fake.calls == []


def test_rename_no_op_is_refused():
    fake = FakeDB(_p84_state())
    with pytest.raises(product_identity.CorrectionRefused, match="must change every"):
        _rename(fake, 84, "fish tofu", "deli")
    assert fake.state["product_corrections"] == []


@pytest.mark.parametrize("status, message", [("merged", "already merged"),
                                             ("rejected", "rejected")])
def test_rename_refuses_retired_products(status, message):
    st = _p84_state()
    st["products"][84].update(status=status,
                              merged_into=S if status == "merged" else None)
    with pytest.raises(product_identity.CorrectionRefused, match=message):
        _rename(FakeDB(st), 84, "fish tofu")


def test_rename_of_a_proposed_product_is_allowed_and_points_at_review():
    st = _p84_state()
    st["products"][84]["status"] = "proposed"
    out = _rename(FakeDB(st), 84, "fish tofu")
    assert "/admin/review" in out["attribute_review"]["note"]


def test_rename_missing_product_is_not_found():
    with pytest.raises(product_identity.ProductNotFound):
        _rename(FakeDB(), 999, "anything")


# ==========================================================================
# Merge: the no-loosening rule
# ==========================================================================

@pytest.mark.parametrize("absorbed, survivor, loosens", [
    (True, True, False),
    (True, None, True),      # NULL would reopen True to the unaudited fill path
    (True, False, True),
    (None, True, False),
    (None, None, False),
    (None, False, True),
    (False, True, False),
    (False, None, False),
    (False, False, False),
])
def test_loosening_rule_ranks_true_over_null_over_false(absorbed, survivor, loosens):
    attr = products.ATTRIBUTES[-1]
    a = {**_attrs(), attr: absorbed}
    s = {**_attrs(), attr: survivor}
    assert product_identity.loosening_attributes(a, s) == ([attr] if loosens else [])


def test_loosening_rule_covers_every_attribute(monkeypatch):
    """A parallel change adds contains_gluten; the rule must see it unchanged."""
    # dict.fromkeys: a no-op once #25 has landed contains_gluten for real.
    monkeypatch.setattr(products, "ATTRIBUTES", tuple(
        dict.fromkeys(products.ATTRIBUTES + ("contains_gluten",))))
    a = {**_attrs(), "contains_gluten": True}
    s = {**_attrs(), "contains_gluten": False}
    assert product_identity.loosening_attributes(a, s) == ["contains_gluten"]


def test_merge_into_a_less_restrictive_survivor_is_refused_with_both_remedies():
    fake = FakeDB(_state(survivor={"contains_dairy": False}))
    before = copy.deepcopy(fake.state)
    with pytest.raises(product_identity.CorrectionRefused, match="loosen") as err:
        _merge(fake)
    msg = str(err.value)
    assert "contains_dairy: absorbed True, survivor False" in msg
    assert f"/admin/products/{S}/attribute-corrections" in msg
    assert f"/admin/products/{A}/attribute-corrections" in msg
    assert fake.state == before and fake.writes() == []


def test_merge_refuses_survivor_null_where_absorbed_is_true():
    fake = FakeDB(_state(absorbed={"contains_animal_byproduct": True},
                         survivor={"contains_animal_byproduct": None}))
    with pytest.raises(product_identity.CorrectionRefused,
                       match="contains_animal_byproduct"):
        _merge(fake)


@pytest.mark.parametrize("absorbed, survivor, loosens", [
    (True, None, True), (True, False, True), (None, False, True),
    (None, True, False), (False, None, False), (False, True, False),
    (True, True, False), (None, None, False), (False, False, False),
])
def test_not_kosher_ranks_true_over_null_over_false(absorbed, survivor, loosens):
    a = {**_attrs(), "not_kosher": absorbed}
    s = {**_attrs(), "not_kosher": survivor}
    assert product_identity.loosening_attributes(a, s) == (
        ["not_kosher"] if loosens else [])


def test_merge_refuses_a_survivor_less_kosher_restrictive_than_the_absorbed():
    fake = FakeDB(_state(absorbed={"not_kosher": True},
                         survivor={"not_kosher": None}))
    with pytest.raises(product_identity.CorrectionRefused, match="not_kosher"):
        _merge(fake)
    fake = FakeDB(_state(absorbed={"not_kosher": None},
                         survivor={"not_kosher": False}))
    with pytest.raises(product_identity.CorrectionRefused, match="not_kosher"):
        _merge(fake)


def test_equal_attribute_merge_has_zero_newly_servable_and_zero_withheld():
    out = _merge(FakeDB())
    impact = out["impact"]
    assert all(d["newly_servable_for"] == [] for d in impact["deals"])
    assert all(d["newly_withheld_for"] == [] for d in impact["deals"])
    assert impact["users"] == []
    assert impact["product_clears"]["newly_cleared"] == []
    assert [d["deal_id"] for d in impact["deals"]] == [100, 101, 102]


def test_tightening_merge_is_allowed_and_reports_withheld_only():
    # Survivor also has egg; absorbed does not. Vegans lose A's deals.
    fake = FakeDB(_state(survivor={"contains_egg": True}))
    out = _merge(fake, dry_run=True)
    deals = {d["deal_id"]: d for d in out["impact"]["deals"]}
    assert deals[100]["newly_withheld_for"] == []      # dairy already fails vegan
    assert all(d["newly_servable_for"] == [] for d in deals.values())
    assert out["impact"]["users"] == []


def test_tightening_merge_withholds_from_vegetarians():
    # Absorbed is plain crackers-like (no dairy); survivor contains fish.
    st = _state(absorbed={"contains_dairy": False},
                survivor={"contains_dairy": False, "contains_fish": True})
    out = _merge(FakeDB(st), dry_run=True)
    deals = {d["deal_id"]: d for d in out["impact"]["deals"]}
    assert deals[100]["newly_withheld_for"] == ["vegan", "vegetarian"]
    assert deals[102]["newly_withheld_for"] == ["vegan", "vegetarian"]
    # 101 already linked the survivor: no flip.
    assert deals[101]["newly_withheld_for"] == []
    users = {u["user_id"]: u for u in out["impact"]["users"]}
    assert users[1]["newly_withheld_deal_ids"] == [100, 102]
    assert users[2]["newly_withheld_deal_ids"] == [100, 102]
    assert 3 not in users and 4 not in users
    assert all(u["newly_servable_deal_ids"] == [] for u in users.values())


def test_guard_trips_if_the_rule_ever_lets_a_loosening_through(monkeypatch):
    monkeypatch.setattr(product_identity, "loosening_attributes", lambda a, s: [])
    fake = FakeDB(_state(survivor={"contains_dairy": False}))
    before = copy.deepcopy(fake.state)
    with pytest.raises(product_identity.LooseningGuardTripped, match="bug"):
        _merge(fake)
    assert fake.state == before and fake.writes() == []


# ==========================================================================
# Merge: the write
# ==========================================================================

def test_merge_repoints_aliases_and_links_and_retires_the_absorbed_product():
    fake = FakeDB()
    out = _merge(fake)
    st = fake.state

    # Aliases: 1, 4, 5 move; 2 is retired because S already maps its string.
    assert {i: (a["product_id"], a["status"]) for i, a in st["aliases"].items()} == {
        1: (S, "confirmed"), 2: (A, "rejected"), 3: (S, "confirmed"),
        4: (S, "proposed"), 5: (S, "rejected")}
    assert st["aliases"][2]["reviewed_by"] == "divad"

    # Deal links: 100 and 102 move (keeping tile position and pricing); 101
    # already linked S, so A's row is dropped -- no PK conflict.
    assert sorted(st["deal_products"]) == [
        (100, S), (101, S), (102, S), (102, OTHER), (103, S)]
    assert st["deal_products"][(102, S)] == {"tile_position": 1,
                                             "price_is_per_item": True}
    assert st["deals"][100]["product_id"] == S
    assert st["deals"][104]["product_id"] == S
    assert st["snapshots"] == [S, S, S]

    # Retired, never deleted.
    assert st["products"][A]["status"] == "merged"
    assert st["products"][A]["merged_into"] == S
    assert st["products"][S]["status"] == "confirmed"

    assert out["moves"] == {
        "aliases_repointed": [1, 4, 5], "aliases_retired": [2],
        "deal_links_repointed": [100, 102], "deal_links_dropped": [101],
        "legacy_deals_repointed": [100, 104], "snapshots_repointed": 2}
    assert not any(s.startswith("DELETE FROM products") for s in fake.writes())


def test_merge_audit_records_what_moved():
    fake = FakeDB()
    _merge(fake)
    st = fake.state
    [header] = st["product_corrections"]
    assert header["pid"] == A and header["kind"] == "merge"
    assert header["impact"].obj["moves"]["aliases_repointed"] == [1, 4, 5]
    assert st["product_merge_corrections"] == [
        {"cid": 500, "sid": S, "n": 2}]
    moves = {(m["action"], m["row_id"]): m["detail"] for m in st["product_merge_moves"]}
    assert moves == {
        ("alias_repointed", 1): {"status": "confirmed"},
        ("alias_repointed", 4): {"status": "proposed"},
        ("alias_repointed", 5): {"status": "rejected"},
        ("alias_retired", 2): {"old_status": "confirmed", "survivor_alias_id": 3},
        ("deal_link_repointed", 100): None,
        ("deal_link_repointed", 102): None,
        ("deal_link_dropped", 101): {"tile_position": 1, "price_is_per_item": False},
        ("deal_legacy_repointed", 100): None,
        ("deal_legacy_repointed", 104): None,
    }


def test_merge_verdicts_after_the_write_match_the_report():
    fake = FakeDB()
    before = {d: {lab: products.deal_allows(fake.linked(d), [lab])
                  for lab in products.RESTRICTION_ATTRIBUTES}
              for d in (100, 101, 102, 103)}
    _merge(fake)
    after = {d: {lab: products.deal_allows(fake.linked(d), [lab])
                 for lab in products.RESTRICTION_ATTRIBUTES}
             for d in (100, 101, 102, 103)}
    assert after == before


@pytest.mark.parametrize("absorbed, survivor", [(A, S), (S, A)])
def test_merge_locks_both_rows_lower_id_first(absorbed, survivor):
    fake = FakeDB(_state())
    _merge(fake, absorbed, survivor)
    assert fake.locked == [min(A, S), max(A, S)]


def test_merge_dry_run_writes_nothing_takes_no_lock_and_matches_the_real_report():
    fake = FakeDB()
    before = copy.deepcopy(fake.state)
    out = _merge(fake, dry_run=True)
    assert fake.state == before and fake.writes() == [] and fake.locked == []
    assert out["dry_run"] is True and out["correction_id"] is None
    real = _merge(FakeDB())
    assert out["impact"] == real["impact"]
    assert out["moves"] == real["moves"]


@pytest.mark.parametrize("fail_on", [
    "UPDATE products SET status = 'merged'",
    "DELETE FROM deal_products",
    "INSERT INTO product_merge_moves",
])
def test_merge_failure_anywhere_rolls_back_everything(fail_on):
    fake = FakeDB()
    before = copy.deepcopy(fake.state)
    fake.fail_on = [fail_on]
    with pytest.raises(RuntimeError):
        _merge(fake)
    assert fake.rolled_back == 1
    assert fake.state == before


def test_merge_concurrent_link_collision_is_a_refusal_and_rolls_back():
    fake = FakeDB()
    before = copy.deepcopy(fake.state)
    fake.unique_violation_on = "UPDATE deal_products SET product_id"
    with pytest.raises(product_identity.CorrectionRefused, match="concurrent"):
        _merge(fake)
    assert fake.state == before


def test_merge_warns_when_a_staple_flag_would_be_lost():
    fake = FakeDB(_state(absorbed={"assumed_on_hand": True}))
    out = _merge(fake, dry_run=True)
    assert any("assumed_on_hand" in w for w in out["warnings"])


def test_merge_with_no_aliases_or_links_skips_those_statements():
    st = _state()
    st["aliases"] = {}
    st["deal_products"] = {}
    fake = FakeDB(st)
    out = _merge(fake)
    assert out["moves"]["aliases_repointed"] == []
    assert not any(s.startswith("SELECT alias_id, normalized_name, status FROM")
                   for s, _ in fake.calls)
    assert fake.state["products"][A]["status"] == "merged"


# ==========================================================================
# Merge: refusals
# ==========================================================================

def test_self_merge_is_invalid():
    fake = FakeDB()
    with pytest.raises(product_identity.CorrectionInvalid, match="itself"):
        _merge(fake, A, A)
    assert fake.calls == []


def test_merge_into_a_merged_product_is_refused():
    st = _state()
    st["products"][S].update(status="merged", merged_into=OTHER)
    with pytest.raises(product_identity.CorrectionRefused,
                       match=f"merged into product {OTHER}"):
        _merge(FakeDB(st))


def test_merging_an_already_merged_product_is_refused():
    st = _state()
    st["products"][A].update(status="merged", merged_into=OTHER)
    with pytest.raises(product_identity.CorrectionRefused, match="already merged"):
        _merge(FakeDB(st))


def test_merging_a_product_others_were_merged_into_is_a_chain_and_refused():
    st = _state()
    st["products"][40] = _product(40, "old cheddar", status="merged",
                                  contains_dairy=True)
    st["products"][40]["merged_into"] = A
    fake = FakeDB(st)
    before = copy.deepcopy(fake.state)
    with pytest.raises(product_identity.CorrectionRefused, match="chain"):
        _merge(fake)
    assert fake.state == before


@pytest.mark.parametrize("who, status", [(A, "proposed"), (S, "proposed"),
                                         (S, "rejected")])
def test_merge_requires_both_confirmed(who, status):
    st = _state()
    st["products"][who]["status"] = status
    with pytest.raises(product_identity.CorrectionRefused):
        _merge(FakeDB(st))


def test_merge_missing_product_is_not_found():
    with pytest.raises(product_identity.ProductNotFound):
        _merge(FakeDB(), A, 999)


@pytest.mark.parametrize("twin_status", ["proposed", "rejected"])
def test_alias_conflict_review_must_settle_is_refused(twin_status):
    st = _state()
    st["aliases"][3]["status"] = twin_status   # A's copy (2) is confirmed
    fake = FakeDB(st)
    before = copy.deepcopy(fake.state)
    with pytest.raises(product_identity.CorrectionRefused, match="alias 2"):
        _merge(fake)
    assert fake.state == before


def test_alias_conflict_with_unconfirmed_absorbed_copy_is_retired():
    st = _state()
    st["aliases"][2]["status"] = "proposed"
    st["aliases"][3]["status"] = "proposed"
    fake = FakeDB(st)
    _merge(fake)
    assert fake.state["aliases"][2]["status"] == "rejected"
    assert fake.state["aliases"][3]["status"] == "proposed"


# ==========================================================================
# Readers: a merged product is withheld or ignored everywhere
# ==========================================================================

def _merged_row():
    fake = FakeDB()
    _merge(fake)
    return fake.state["products"][A]


def test_gate_withholds_a_deal_still_linked_to_a_merged_product():
    """Should a link to the merged product survive (a race with link_deals),
    the gate fails closed on it for every restricted user."""
    merged = {**_merged_row(), "contains_dairy": False}   # even if "clean"
    for label in products.RESTRICTION_ATTRIBUTES:
        assert not products.deal_allows([merged], [label])
    assert not products.deal_allows([merged], ["keto"])  # unknown label too
    assert products.deal_allows([merged], [])            # unrestricted: open
    fragment, _ = products.compliant_product_filter(["vegan"])
    assert "p.status <> 'confirmed'" in fragment


def test_recipe_checker_rejects_a_merged_product():
    merged = _merged_row()
    vs = dietary._check_products("cheddar", "deal 100", [merged], ["no_nuts"],
                                 ("contains_nuts",))
    assert len(vs) == 1 and "not a confirmed product" in vs[0].reason


def test_sous_chef_is_not_offered_a_merged_staple():
    merged = {**_merged_row(), "contains_dairy": False}
    assert offered_staples([merged], ["vegan"]) == []


class _OneRowCursor:
    def __init__(self, row):
        self.row = row
        self.calls = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append(" ".join(sql.split()))

    def fetchone(self):
        return self.row


class _OneRowDB:
    def __init__(self, row):
        self.cursor = _OneRowCursor(row)

    @contextmanager
    def get_cursor(self):
        yield self.cursor


def test_confirm_group_will_not_revive_a_merged_product():
    db = _OneRowDB({"status": "merged"})
    with pytest.raises(products.ProductRetired):
        products.confirm_group(A, [1], "divad", database=db)
    assert not any(c.startswith("UPDATE") for c in db.cursor.calls)


def test_confirm_group_never_updates_a_merged_product_status():
    db = _OneRowDB({"status": "proposed"})
    products.confirm_group(A, [1], "divad", database=db)
    update = next(c for c in db.cursor.calls if c.startswith("UPDATE products"))
    assert "status NOT IN ('confirmed', 'merged')" in update


def test_set_attributes_refuses_a_merged_product():
    db = _OneRowDB({"status": "merged", "canonical_name": "cheddar",
                    **_attrs(contains_dairy=True)})
    with pytest.raises(products.ProductRetired):
        products.set_attributes(A, {"contains_dairy": False}, database=db)
    assert not any(c.startswith("UPDATE") for c in db.cursor.calls)


def test_fill_undecided_skips_merged_products():
    db = _OneRowDB(None)
    out = products.fill_undecided_attributes(
        A, {"contains_animal_byproduct": False}, database=db)
    assert out == {}
    assert "status <> 'merged'" in db.cursor.calls[0]


def test_proposer_resolves_a_merged_name_to_its_survivor():
    from app.services import product_proposer

    class Cursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append(" ".join(sql.split()))

        def fetchall(self):
            return []

    class DB:
        def __init__(self):
            self.cursor = Cursor()

        @contextmanager
        def get_cursor(self):
            yield self.cursor

    db = DB()
    product_proposer.persist_proposals([], database=db)
    sql = db.cursor.calls[0]
    assert "COALESCE(merged_into, product_id) AS product_id" in sql
    # Live rows sort last so they win the name over merged/rejected ones.
    assert "WHEN 'rejected' THEN 0 WHEN 'merged' THEN 1 ELSE 2" in sql


# ==========================================================================
# HTTP
# ==========================================================================

@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB(_p84_state())
    monkeypatch.setattr(product_identity, "rename_product",
                        partial(product_identity.rename_product, database=db))
    monkeypatch.setattr(product_identity, "merge_products",
                        partial(product_identity.merge_products, database=db))
    return db


RENAME = {"canonical_name": "fish tofu", "category": "seafood",
          "reviewer": "divad", "reason": "the label lists fish paste"}
MERGE = {"into": S, "reviewer": "divad", "reason": "same product"}


@pytest.mark.asyncio
async def test_route_rename_happy_path(client, fake):
    async with client as http:
        res = await http.post("/admin/products/84/identity-corrections", json=RENAME)
    assert res.status_code == 200, res.text
    assert res.json()["attribute_review"]["endpoint"].endswith(
        "/84/attribute-corrections")
    assert fake.state["products"][84]["canonical_name"] == "fish tofu"


@pytest.mark.asyncio
async def test_route_rename_dry_run_writes_nothing(client, fake):
    before = copy.deepcopy(fake.state)
    async with client as http:
        res = await http.post("/admin/products/84/identity-corrections",
                              params={"dry_run": "true"}, json=RENAME)
    assert res.status_code == 200 and res.json()["dry_run"] is True
    assert fake.state == before


@pytest.mark.asyncio
@pytest.mark.parametrize("pid, body, code", [
    (84, {**RENAME, "canonical_name": "cheddar cheese"}, 409),   # collision
    (84, {**RENAME, "canonical_name": "cheese tofu", "category": "deli"}, 409),
    (84, {**RENAME, "canonical_name": " "}, 400),
    (84, {**RENAME, "canonical_name": None, "category": None}, 400),
    (84, {k: v for k, v in RENAME.items() if k != "reason"}, 422),
    (999, RENAME, 404),
])
async def test_route_rename_refusals_write_nothing(client, fake, pid, body, code):
    before = copy.deepcopy(fake.state)
    async with client as http:
        res = await http.post(f"/admin/products/{pid}/identity-corrections", json=body)
    assert res.status_code == code, res.text
    assert fake.state == before


@pytest.mark.asyncio
async def test_route_merge_happy_path(client, fake):
    async with client as http:
        res = await http.post(f"/admin/products/{A}/merge", json=MERGE)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["correction_id"] == 500
    assert out["moves"]["deal_links_dropped"] == [101]
    assert fake.state["products"][A]["status"] == "merged"


@pytest.mark.asyncio
async def test_route_merge_dry_run_writes_nothing(client, fake):
    before = copy.deepcopy(fake.state)
    async with client as http:
        res = await http.post(f"/admin/products/{A}/merge",
                              params={"dry_run": "true"}, json=MERGE)
    assert res.status_code == 200 and res.json()["dry_run"] is True
    assert fake.state == before


@pytest.mark.asyncio
@pytest.mark.parametrize("pid, body, setup, code", [
    (A, {**MERGE, "into": A}, None, 400),                       # self-merge
    (A, {k: v for k, v in MERGE.items() if k != "into"}, None, 422),
    (A, {**MERGE, "reviewer": " "}, None, 400),
    (A, {**MERGE, "into": 999}, None, 404),
    (A, MERGE, ("survivor", {"contains_dairy": False}), 409),   # loosening
    (A, MERGE, ("survivor", {"status": "merged", "merged_into": OTHER}), 409),
])
async def test_route_merge_refusals_write_nothing(client, fake, pid, body, setup, code):
    if setup:
        role, change = setup
        fake.state["products"][S if role == "survivor" else A].update(change)
    before = copy.deepcopy(fake.state)
    async with client as http:
        res = await http.post(f"/admin/products/{pid}/merge", json=body)
    assert res.status_code == code, res.text
    assert fake.state == before


@pytest.mark.asyncio
async def test_route_merge_guard_is_a_500_and_writes_nothing(client, fake, monkeypatch):
    monkeypatch.setattr(product_identity, "loosening_attributes", lambda a, s: [])
    fake.state["products"][S]["contains_dairy"] = False
    before = copy.deepcopy(fake.state)
    async with client as http:
        res = await http.post(f"/admin/products/{A}/merge", json=MERGE)
    assert res.status_code == 500 and "guard" in res.json()["detail"]
    assert fake.state == before


@pytest.mark.asyncio
async def test_route_decide_refuses_to_confirm_onto_a_merged_product(client, monkeypatch):
    def _confirm(*a, **kw):
        raise products.ProductRetired(A, S)

    monkeypatch.setattr(products, "confirm_group", _confirm)
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": A, "alias_ids": [2], "action": "confirm",
            "reviewer": "divad"})
    assert res.status_code == 409
    assert f"product {S}" in res.json()["detail"]
