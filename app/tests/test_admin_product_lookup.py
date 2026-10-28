"""Read-only admin lookup: GET /admin/products and GET /admin/products/{id}.

No database. A scripted fake stands in for ``db``; the real service functions
run against it, so SQL shape, row handling and the HTTP contract are exercised
together. Driven through ``httpx.ASGITransport`` (see CLAUDE.md).
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path

import httpx
import pytest

from app.main import app
from app.services import products

pytestmark = pytest.mark.asyncio


PAGE = Path(__file__).resolve().parents[1] / "static" / "review.html"


def _attrs(**overrides):
    row = {a: False for a in products.ATTRIBUTES}
    row.update(overrides)
    return row


def _prod(pid, name, status="confirmed", merged_into=None, **attr):
    return {"product_id": pid, "canonical_name": name, "category": "pantry",
            "status": status, "merged_into": merged_into,
            "is_pantry_staple": False, "assumed_on_hand": False, **_attrs(**attr)}


PRODUCTS = [
    _prod(1, "Oat Milk"),
    _prod(2, "Whole Milk", contains_dairy=True),
    _prod(3, "100% Juice"),
    _prod(4, "Old Milk", status="merged", merged_into=2, contains_dairy=True),
    _prod(5, "Mystery", status="proposed"),
]

WHEN = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, fake):
        self.fake = fake
        self._one = None
        self._all = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.fake.calls.append((s, params))
        assert s.startswith("SELECT"), f"lookup must be read-only: {s[:60]}"
        if s.startswith("SELECT count(*)"):
            self._one = {"n": len(self.fake.matching(params))}
        elif "FROM products WHERE product_id" in s:
            pid = params["pid"]
            self._one = next((dict(p) for p in PRODUCTS if p["product_id"] == pid), None)
        elif "FROM products" in s:
            rows = self.fake.matching(params)
            cols = ("product_id", "canonical_name", "category", "status",
                    "merged_into", "is_pantry_staple")
            self._all = [{c: r[c] for c in cols} for r in
                         rows[params["offset"]: params["offset"] + params["limit"]]]
        elif "FROM product_aliases" in s:
            self._all = [dict(a) for a in self.fake.aliases]
        elif "FROM deal_products" in s:
            self._all = [dict(d) for d in self.fake.deals][: params["lim"]]
        elif "FROM product_attribute_correction_log" in s:
            self._all = [dict(r) for r in self.fake.attr_log]
        elif "FROM product_identity_correction_log" in s:
            self._all = [dict(r) for r in self.fake.identity_log]
        elif "FROM product_merge_log" in s:
            self._all = [dict(r) for r in self.fake.merge_log]
        else:  # pragma: no cover
            raise AssertionError(f"unscripted SQL: {s[:80]}")

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeDB:
    def __init__(self):
        self.calls = []
        self.aliases = [{"alias_id": 11, "normalized_name": "oat milk",
                         "raw_example": "OAT MILK 1L", "status": "confirmed"}]
        self.deals = [
            {"deal_id": 50, "product_name": "oat milk 1L",
             "valid_until": date(2026, 11, 3), "live": True},
            {"deal_id": 49, "product_name": "oat milk",
             "valid_until": date(2026, 10, 1), "live": False},
        ]
        self.attr_log = [{"correction_id": 3, "corrected_at": WHEN, "reviewer": "d",
                          "attribute": "contains_nuts", "old_value": True,
                          "new_value": False, "reason": "oats"}]
        self.identity_log = [{"correction_id": 4, "corrected_at": WHEN, "reviewer": "d",
                              "field": "canonical_name", "old_value": "oat drink",
                              "new_value": "Oat Milk", "reason": "name"}]
        self.merge_log = []

    def matching(self, params):
        rows = list(PRODUCTS)
        if "status" in params:
            rows = [r for r in rows if r["status"] == params["status"]]
        if "q" in params:
            needle = params["q"][1:-1].lower()
            needle = needle.replace("\\%", "%").replace("\\_", "_")
            rows = [r for r in rows if needle in r["canonical_name"].lower()]
        return sorted(rows, key=lambda r: (r["canonical_name"].lower(), r["product_id"]))

    @contextmanager
    def get_cursor(self, *a, **kw):
        yield FakeCursor(self)


@pytest.fixture
def fake(monkeypatch):
    f = FakeDB()
    monkeypatch.setattr(products, "product_detail",
                        partial(products.product_detail, database=f))
    monkeypatch.setattr(products, "list_products",
                        partial(products.list_products, database=f))
    return f


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_detail_contract(client, fake):
    res = await client.get("/admin/products/1")
    assert res.status_code == 200
    d = res.json()
    for key in ("product_id", "canonical_name", "category", "status", "merged_into",
                "is_pantry_staple", "assumed_on_hand", "attributes", "clears",
                "aliases", "deals", "history"):
        assert key in d
    assert d["canonical_name"] == "Oat Milk" and d["status"] == "confirmed"
    assert d["merged_into"] is None
    assert d["aliases"] == [{"alias_id": 11, "normalized_name": "oat milk",
                             "raw_example": "OAT MILK 1L", "status": "confirmed"}]
    assert [x["deal_id"] for x in d["deals"]] == [50, 49]
    assert [x["live"] for x in d["deals"]] == [True, False]
    assert d["deals"][0]["valid_until"] == "2026-11-03"
    assert d["history"]["attributes"][0]["attribute"] == "contains_nuts"
    assert d["history"]["attributes"][0]["corrected_at"] == WHEN.isoformat()
    assert d["history"]["identity"][0]["field"] == "canonical_name"
    assert d["history"]["merges"] == []
    assert d["clears"] == products.clears_labels(
        {"product_id": 1, **d["attributes"]})
    assert "vegan" in d["clears"]


async def test_attributes_iterate_products_attributes(client, fake, monkeypatch):
    # A new attribute appearing in ATTRIBUTES must flow through untouched.
    monkeypatch.setattr(products, "ATTRIBUTES",
                        products.ATTRIBUTES + ("contains_halal_unsafe",))
    monkeypatch.setattr(products, "_ATTR_SELECT",
                        products._ATTR_SELECT + ", contains_halal_unsafe")
    monkeypatch.setitem(PRODUCTS[0], "contains_halal_unsafe", True)
    try:
        d = (await client.get("/admin/products/1")).json()
    finally:
        del PRODUCTS[0]["contains_halal_unsafe"]
    assert list(d["attributes"]) == list(products.ATTRIBUTES)
    assert d["attributes"]["contains_halal_unsafe"] is True
    assert "contains_halal_unsafe" in fake.calls[0][0]


async def test_detail_all_current_attributes(client, fake):
    d = (await client.get("/admin/products/2")).json()
    assert list(d["attributes"]) == list(products.ATTRIBUTES)
    assert d["attributes"]["contains_dairy"] is True
    assert "vegan" not in d["clears"] and "vegetarian" in d["clears"]


async def test_detail_404(client, fake):
    res = await client.get("/admin/products/999")
    assert res.status_code == 404
    assert len(fake.calls) == 1   # stops at the missing product


async def test_detail_merged_product_is_returned(client, fake):
    fake.merge_log = [{"correction_id": 9, "corrected_at": WHEN, "reviewer": "d",
                       "absorbed_product_id": 4, "absorbed_name": "Old Milk",
                       "survivor_product_id": 2, "survivor_name": "Whole Milk",
                       "snapshots_repointed": 3, "rows_moved": 5, "reason": "dup"}]
    d = (await client.get("/admin/products/4")).json()
    assert d["status"] == "merged" and d["merged_into"] == 2
    assert d["history"]["merges"][0]["role"] == "absorbed"
    s = (await client.get("/admin/products/2")).json()
    assert s["history"]["merges"][0]["role"] == "survivor"


async def test_detail_is_read_only(client, fake):
    await client.get("/admin/products/1")
    assert all(sql.startswith("SELECT") for sql, _ in fake.calls)


async def test_list_defaults_to_confirmed(client, fake):
    d = (await client.get("/admin/products")).json()
    assert d["status"] == "confirmed" and d["total"] == 3
    assert [i["canonical_name"] for i in d["items"]] == [
        "100% Juice", "Oat Milk", "Whole Milk"]
    assert set(d["items"][0]) == {"product_id", "canonical_name", "category",
                                  "status", "merged_into", "is_pantry_staple"}


@pytest.mark.parametrize("status,ids", [
    ("merged", [4]), ("proposed", [5]), ("rejected", []),
    ("all", [3, 5, 1, 4, 2]),
])
async def test_list_status_filter(client, fake, status, ids):
    d = (await client.get("/admin/products", params={"status": status})).json()
    assert [i["product_id"] for i in d["items"]] == ids


async def test_list_unknown_status_is_400(client, fake):
    assert (await client.get("/admin/products?status=bogus")).status_code == 400


async def test_list_q_is_case_insensitive_substring(client, fake):
    d = (await client.get("/admin/products", params={"q": "MILK"})).json()
    assert [i["product_id"] for i in d["items"]] == [1, 2] and d["total"] == 2
    # wildcard characters are literals, not patterns
    d = (await client.get("/admin/products", params={"q": "100%"})).json()
    assert [i["product_id"] for i in d["items"]] == [3]
    sql, params = [c for c in fake.calls if "LIMIT" in c[0]][-1]
    assert params["q"] == "%100\\%%"


async def test_list_paging(client, fake):
    first = (await client.get("/admin/products?limit=2&offset=0")).json()
    second = (await client.get("/admin/products?limit=2&offset=2")).json()
    assert first["total"] == second["total"] == 3
    assert [i["product_id"] for i in first["items"]] == [3, 1]
    assert [i["product_id"] for i in second["items"]] == [2]
    assert (await client.get("/admin/products?limit=0")).status_code == 422
    assert (await client.get("/admin/products?limit=501")).status_code == 422
    assert (await client.get("/admin/products?offset=-1")).status_code == 422


async def test_queue_route_not_shadowed_by_detail(client, fake, monkeypatch):
    monkeypatch.setattr(products, "pending_proposals", lambda **kw: [])
    monkeypatch.setattr(products, "undecided_products", lambda **kw: [])
    monkeypatch.setattr(products, "review_stats", lambda **kw: {})
    assert (await client.get("/admin/products/queue")).status_code == 200


async def test_page_uses_lookup_endpoints():
    html = PAGE.read_text(encoding="utf-8")
    assert "/admin/products?status=confirmed" in html
    assert "`/admin/products/${pid}`" in html
    # the preview -> confirm gate is still the only writer
    assert "corrPreview" in html and "corrConfirm" in html
