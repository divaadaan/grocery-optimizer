"""Attribute corrections on confirmed products: the audited path.

No database, network or LLM. A scripted fake stands in for ``db``: it answers
the service's reads from fixtures, and buffers writes per ``get_cursor`` block
the way the real pool does -- committed when the block exits cleanly, dropped
when it raises -- so "atomic" and "dry run writes nothing" are observable.

The HTTP layer is driven through ``httpx.ASGITransport`` (see CLAUDE.md) with
the real service bound to the fake database, so these exercise validation,
impact and the write together rather than a stub of them.
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import partial

import httpx
import pytest

from app.main import app
from app.services import product_corrections, products

PID = 7


def _attrs(**overrides):
    row = {a: False for a in products.ATTRIBUTES}
    row.update(overrides)
    return row


def _product(**overrides):
    # A confirmed product mislabelled as containing nuts.
    row = {"product_id": PID, "canonical_name": "oat granola",
           "category": "pantry", "status": "confirmed",
           "is_pantry_staple": False, **_attrs(contains_nuts=True)}
    row.update(overrides)
    return row


def _deal_row(deal_id, linked, live=True, status="confirmed", **attrs):
    return {"deal_id": deal_id, "product_name": f"tile {deal_id}",
            "store_id": 1, "valid_from": date(2026, 10, 20),
            "valid_until": date(2026, 11, 3) if live else date(2026, 10, 1),
            "live": live, "linked_product_id": linked,
            "linked_status": status, **_attrs(**attrs)}


def _deal_rows(product):
    stored = {a: product[a] for a in products.ATTRIBUTES}
    return [
        # 100: this product alone, live.
        {**_deal_row(100, PID), **stored},
        # 101: a tile with this product AND a proposed one -> withheld from
        # every restricted user before and after, whatever we change.
        {**_deal_row(101, PID), **stored},
        _deal_row(101, 8, status="proposed"),
        # 102: this product alone, expired.
        {**_deal_row(102, PID, live=False), **stored},
    ]


USERS = [
    {"user_id": 1, "dietary_restrictions": ["no_nuts"]},
    {"user_id": 2, "dietary_restrictions": ["vegan"]},
    {"user_id": 3, "dietary_restrictions": ["vegetarian", "nut_free"]},
    {"user_id": 4, "dietary_restrictions": []},
]


class FakeCursor:
    def __init__(self, fake):
        self.fake = fake
        self._one = None
        self._all = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.fake.calls.append((s, params))
        if s.startswith("SELECT product_id, canonical_name, category, status"):
            self._one = dict(self.fake.product) if self.fake.product else None
        elif s.startswith("SELECT status, canonical_name"):   # set_attributes
            self._one = dict(self.fake.product) if self.fake.product else None
        elif "FROM deal_products target" in s:
            self._all = [dict(r) for r in self.fake.deal_rows]
        elif "FROM users" in s:
            self._all = [dict(r) for r in self.fake.users]
        elif s.startswith("INSERT INTO product_corrections"):
            self.fake.pending.append(("product_corrections", params))
            self._one = {"correction_id": 41,
                         "corrected_at": datetime(2026, 10, 28, tzinfo=timezone.utc)}
        elif s.startswith("INSERT INTO product_attribute_corrections"):
            self.fake.pending.append(("product_attribute_corrections", params))
        elif s.startswith("UPDATE products"):
            if self.fake.fail_update:
                raise RuntimeError("connection lost")
            self.fake.pending.append(("UPDATE products", (s, params)))
            self.rowcount = 1
        else:  # pragma: no cover - a new query should be scripted, not ignored
            raise AssertionError(f"unscripted SQL: {s[:80]}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeDB:
    def __init__(self, product=None, deal_rows=None, users=USERS):
        self.product = product if product is not None else _product()
        if deal_rows is None:
            deal_rows = _deal_rows(self.product) if self.product else []
        self.deal_rows = deal_rows
        self.users = users
        self.calls = []
        self.pending = []
        self.committed = []
        self.transactions = 0
        self.rolled_back = 0
        self.fail_update = False

    @contextmanager
    def get_cursor(self):
        self.transactions += 1
        try:
            yield FakeCursor(self)
        except BaseException:
            self.pending.clear()
            self.rolled_back += 1
            raise
        else:
            self.committed.extend(self.pending)
            self.pending.clear()

    def written(self, table):
        return [p for t, p in self.committed if t == table]


def _correct(fake, changes, reviewer="divad", reason="label says oats only",
             dry_run=False):
    return product_corrections.correct_attributes(
        PID, changes, reviewer, reason, dry_run=dry_run, database=fake)


# --------------------------------------------------------------------------
# Service: the write
# --------------------------------------------------------------------------

def test_happy_path_writes_audit_rows_and_update_in_one_transaction():
    fake = FakeDB()
    out = _correct(fake, {"contains_nuts": False})

    assert fake.transactions == 1 and fake.rolled_back == 0
    [header] = fake.written("product_corrections")
    assert header["pid"] == PID and header["kind"] == "attribute"
    assert header["who"] == "divad" and header["why"] == "label says oats only"
    assert header["impact"].obj == out["impact"]       # report stored with it
    assert fake.written("product_attribute_corrections") == [{
        "cid": 41, "attribute": "contains_nuts",
        "old_value": True, "new_value": False}]
    [(sql, params)] = fake.written("UPDATE products")
    assert "contains_nuts = %(contains_nuts)s" in sql
    assert "status = 'confirmed'" in sql
    assert params == {"contains_nuts": False, "pid": PID}

    assert out["dry_run"] is False and out["correction_id"] == 41
    assert out["corrected_at"].startswith("2026-10-28")
    assert out["changes"] == [{"attribute": "contains_nuts",
                               "old_value": True, "new_value": False}]
    assert out["product"]["before"]["contains_nuts"] is True
    assert out["product"]["after"]["contains_nuts"] is False
    # The real write locks the row it checks.
    assert "FOR UPDATE" in fake.calls[0][0]


def test_dry_run_writes_nothing_and_takes_no_lock():
    fake = FakeDB()
    out = _correct(fake, {"contains_nuts": False}, dry_run=True)
    assert fake.committed == [] and fake.pending == []
    assert not any(s.startswith(("INSERT", "UPDATE")) for s, _ in fake.calls)
    assert "FOR UPDATE" not in fake.calls[0][0]
    assert out["dry_run"] is True and out["correction_id"] is None
    # Same report as the real thing.
    real = _correct(FakeDB(), {"contains_nuts": False})
    assert out["impact"] == real["impact"]


def test_failed_update_rolls_back_the_audit_rows():
    fake = FakeDB()
    fake.fail_update = True
    with pytest.raises(RuntimeError):
        _correct(fake, {"contains_nuts": False})
    assert fake.rolled_back == 1 and fake.committed == []


def test_several_attributes_are_one_event_with_one_row_each():
    fake = FakeDB()
    _correct(fake, {"contains_dairy": True, "contains_nuts": False})
    assert len(fake.written("product_corrections")) == 1
    rows = fake.written("product_attribute_corrections")
    assert [(r["attribute"], r["old_value"], r["new_value"]) for r in rows] == [
        ("contains_dairy", False, True), ("contains_nuts", True, False)]


def test_correcting_an_undecided_attribute_records_old_value_null():
    fake = FakeDB(product=_product(contains_animal_byproduct=None))
    _correct(fake, {"contains_animal_byproduct": True})
    [row] = fake.written("product_attribute_corrections")
    assert row["old_value"] is None and row["new_value"] is True


# --------------------------------------------------------------------------
# Service: refusals, all before any write
# --------------------------------------------------------------------------

@pytest.mark.parametrize("changes, message", [
    ({"contains_gelatin": True}, "unknown attribute"),
    ({"canonical_name": "x"}, "unknown attribute"),
    ({}, "no attribute changes"),
    ({"contains_nuts": None}, "null"),
    ({"contains_nuts": "false"}, "true or false"),
    ({"contains_nuts": 0}, "true or false"),
])
def test_invalid_changes_are_refused(changes, message):
    fake = FakeDB()
    with pytest.raises(product_corrections.CorrectionInvalid, match=message):
        _correct(fake, changes)
    assert fake.calls == []


@pytest.mark.parametrize("reviewer, reason", [
    ("", "a reason"), ("   ", "a reason"), (None, "a reason"),
    ("divad", ""), ("divad", "  "), ("divad", None),
])
def test_reviewer_and_reason_are_required(reviewer, reason):
    fake = FakeDB()
    with pytest.raises(product_corrections.CorrectionInvalid, match="required"):
        _correct(fake, {"contains_nuts": False}, reviewer=reviewer, reason=reason)
    assert fake.calls == []


@pytest.mark.parametrize("status", ["proposed", "rejected"])
def test_unconfirmed_products_are_refused(status):
    fake = FakeDB(product=_product(status=status))
    with pytest.raises(product_corrections.CorrectionRefused, match="not confirmed"):
        _correct(fake, {"contains_nuts": False})
    assert fake.committed == []


def test_missing_product_is_not_found():
    fake = FakeDB(product={})
    with pytest.raises(product_corrections.ProductNotFound):
        _correct(fake, {"contains_nuts": False})


def test_no_op_is_refused_even_alongside_a_real_change():
    fake = FakeDB()
    with pytest.raises(product_corrections.CorrectionRefused,
                       match="already stores contains_dairy=False"):
        _correct(fake, {"contains_nuts": False, "contains_dairy": False})
    assert fake.committed == []


# --------------------------------------------------------------------------
# Impact report
# --------------------------------------------------------------------------

def _deal(out, deal_id):
    return next(d for d in out["impact"]["deals"] if d["deal_id"] == deal_id)


def test_impact_of_clearing_a_wrong_nuts_flag():
    out = _correct(FakeDB(), {"contains_nuts": False}, dry_run=True)
    impact = out["impact"]

    assert impact["product_clears"]["newly_cleared"] == ["no_nuts", "nut_free"]
    assert impact["product_clears"]["newly_failing"] == []
    assert "vegan" in impact["product_clears"]["before"]

    assert _deal(out, 100)["newly_servable_for"] == ["no_nuts", "nut_free"]
    assert _deal(out, 100)["newly_withheld_for"] == []
    # A proposed product on the same tile keeps it withheld: no flip.
    assert _deal(out, 101)["newly_servable_for"] == []
    assert _deal(out, 101)["other_products"] == [8]
    # Expired deals are reported but serve nobody.
    assert _deal(out, 102)["live"] is False
    assert _deal(out, 102)["newly_servable_for"] == ["no_nuts", "nut_free"]

    # Vegan (user 2) never read contains_nuts; user 4 has no restriction.
    assert impact["users"] == [
        {"user_id": 1, "dietary_restrictions": ["no_nuts"],
         "newly_withheld_deal_ids": [], "newly_servable_deal_ids": [100]},
        {"user_id": 3, "dietary_restrictions": ["vegetarian", "nut_free"],
         "newly_withheld_deal_ids": [], "newly_servable_deal_ids": [100]},
    ]
    assert impact["summary"] == {
        "deals_linked": 3, "deals_live": 2, "deals_changed": 2,
        "live_deals_changed": 1, "users_affected": 2}


def test_impact_of_adding_dairy_withholds_from_vegans():
    product = _product(contains_nuts=False)
    out = _correct(FakeDB(product=product), {"contains_dairy": True},
                   dry_run=True)
    assert out["impact"]["product_clears"]["newly_failing"] == [
        "dairy_free", "vegan"]
    assert _deal(out, 100)["newly_withheld_for"] == ["dairy_free", "vegan"]
    assert [u["user_id"] for u in out["impact"]["users"]] == [2]
    assert out["impact"]["users"][0]["newly_withheld_deal_ids"] == [100]


def test_impact_with_no_linked_deals_or_users():
    out = _correct(FakeDB(deal_rows=[], users=[]), {"contains_nuts": False},
                   dry_run=True)
    assert out["impact"]["deals"] == [] and out["impact"]["users"] == []
    assert out["impact"]["summary"]["deals_linked"] == 0


def test_a_new_attribute_and_label_flow_through_without_code_changes(monkeypatch):
    """A parallel change adds contains_gluten; nothing here may hardcode the
    attribute list or the label list."""
    monkeypatch.setattr(products, "ATTRIBUTES",
                        products.ATTRIBUTES + ("contains_gluten",))
    monkeypatch.setattr(products, "RESTRICTION_ATTRIBUTES", {
        **products.RESTRICTION_ATTRIBUTES, "gluten_free": ("contains_gluten",)})
    product = _product(contains_nuts=False, contains_gluten=True)
    fake = FakeDB(product=product,
                  users=[{"user_id": 9, "dietary_restrictions": ["gluten_free"]}])
    out = _correct(fake, {"contains_gluten": False})
    assert out["impact"]["product_clears"]["newly_cleared"] == ["gluten_free"]
    assert _deal(out, 100)["newly_servable_for"] == ["gluten_free"]
    assert out["impact"]["users"][0]["newly_servable_deal_ids"] == [100]
    assert "contains_gluten" in fake.calls[0][0]          # read
    assert fake.written("product_attribute_corrections")[0]["attribute"] == \
        "contains_gluten"


def test_deal_allows_matches_the_sql_gate_rules():
    ok = {"product_id": 1, "status": "confirmed", **_attrs()}
    dairy = {**ok, "product_id": 2, "contains_dairy": True}
    undecided = {**ok, "product_id": 3, "contains_animal_byproduct": None}
    assert products.deal_allows([], None)                      # unrestricted
    assert not products.deal_allows([], ["vegan"])             # unlinked
    assert products.deal_allows([ok], ["vegan"])
    assert not products.deal_allows([ok, dairy], ["vegan"])    # every product
    assert products.deal_allows([ok, dairy], ["vegetarian"])
    assert not products.deal_allows([undecided], ["vegan"])    # NULL fails closed
    assert not products.deal_allows([{**ok, "status": "proposed"}], ["vegetarian"])
    # An unknown label enforces no attribute but still needs confirmed links.
    assert products.deal_allows([ok], ["keto"])
    assert not products.deal_allows([{**ok, "status": "proposed"}], ["keto"])


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


@pytest.fixture
def fake(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(product_corrections, "correct_attributes",
                        partial(product_corrections.correct_attributes,
                                database=db))
    return db


URL = f"/admin/products/{PID}/attribute-corrections"
BODY = {"changes": {"contains_nuts": False}, "reviewer": "divad",
        "reason": "the label lists oats, honey-free, no nuts"}


@pytest.mark.asyncio
async def test_route_happy_path(client, fake):
    async with client as http:
        res = await http.post(URL, json=BODY)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["correction_id"] == 41 and out["dry_run"] is False
    assert out["impact"]["summary"]["users_affected"] == 2
    assert len(fake.written("product_corrections")) == 1
    assert len(fake.written("UPDATE products")) == 1


@pytest.mark.asyncio
async def test_route_dry_run_writes_nothing(client, fake):
    async with client as http:
        res = await http.post(URL, params={"dry_run": "true"}, json=BODY)
    assert res.status_code == 200, res.text
    assert res.json()["dry_run"] is True
    assert res.json()["impact"]["deals"][0]["newly_servable_for"] == [
        "no_nuts", "nut_free"]
    assert fake.committed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body, product, code", [
    ({**BODY, "changes": {"contains_gluten_free_ish": True}}, None, 400),
    ({**BODY, "changes": {"contains_nuts": None}}, None, 400),
    ({**BODY, "reason": " "}, None, 400),
    ({k: v for k, v in BODY.items() if k != "reviewer"}, None, 422),
    ({k: v for k, v in BODY.items() if k != "reason"}, None, 422),
    (BODY, _product(status="proposed"), 409),
    ({**BODY, "changes": {"contains_nuts": True}}, None, 409),   # no-op
    (BODY, {}, 404),
])
async def test_route_refusals_write_nothing(client, fake, body, product, code):
    if product is not None:
        fake.product = product
    async with client as http:
        res = await http.post(URL, json=body)
    assert res.status_code == code, res.text
    assert fake.committed == []


@pytest.mark.asyncio
async def test_confirm_path_still_refuses_to_change_a_confirmed_attribute(
        client, monkeypatch):
    """PR #24 stays intact: the review flow is not a back door around the
    audited correction, and its 409 now points at the correction endpoint."""
    db = FakeDB()            # product 7 is confirmed with contains_nuts=True
    confirms = []
    monkeypatch.setattr(products, "set_attributes",
                        partial(products.set_attributes, database=db))
    monkeypatch.setattr(products, "confirm_group",
                        lambda *a, **kw: confirms.append(a) or 1)
    async with client as http:
        res = await http.post("/admin/products/decide", json={
            "product_id": PID, "alias_ids": [70], "action": "confirm",
            "reviewer": "divad", "attributes": {"contains_nuts": False}})
    assert res.status_code == 409
    assert "attribute-corrections" in res.json()["detail"]
    assert confirms == []
    assert db.committed == []
    assert not any(s.startswith("UPDATE") for s, _ in db.calls)
