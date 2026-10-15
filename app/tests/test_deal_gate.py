"""The dietary gate: which deals reach a user with restrictions.

A restricted user is served only deals whose linked products are all confirmed
and whose curated attributes clear the restrictions. Product names play no
part: these tests pin that a correctly-labelled "Peanut Butter" reaches a vegan
and "Beefsteak Tomatoes" reaches a vegetarian, while unlinked, proposed,
rejected and violating products are withheld whatever their source.

The SQL fragment is executed for real against an in-memory SQLite schema with
the same shape as ``deals`` / ``deal_products`` / ``products``, so the
semantics are tested, not just the text. No Postgres is touched.
"""
import sqlite3

import pytest

from app.config import settings
from app.services import deal_gate, products

# --------------------------------------------------------------------------
# A tiny catalogue, executed through the real fragment
# --------------------------------------------------------------------------

#: (product_id, canonical_name, status, meat, fish, dairy, egg, nuts,
#:  animal_byproduct). The last is nullable (migration 015); None is a product
#:  confirmed before the column existed and never revisited.
PRODUCTS = [
    (1, "peanut butter", "confirmed", 0, 0, 0, 0, 1, 0),
    (2, "beefsteak tomatoes", "confirmed", 0, 0, 0, 0, 0, 0),
    (3, "butternut squash", "confirmed", 0, 0, 0, 0, 0, 0),
    (4, "prosciutto", "confirmed", 1, 0, 0, 0, 0, None),
    (5, "marzipan", "confirmed", 0, 0, 0, 0, 1, 0),
    (6, "milk", "confirmed", 0, 0, 1, 0, 0, None),
    (7, "tofu", "proposed", 0, 0, 0, 0, 0, 0),
    (8, "lentils", "rejected", 0, 0, 0, 0, 0, 0),
    (9, "chicken breast", "confirmed", 1, 0, 0, 0, 0, None),
    (10, "honey", "confirmed", 0, 0, 0, 0, 0, 1),
    (11, "strawberry jam", "confirmed", 0, 0, 0, 0, 0, None),
]

#: (deal_id, product_name as printed, source, [linked product ids])
DEALS = [
    (101, "KRAFT PEANUT BUTTER 1KG", "flipp", [1]),
    (102, "Beefsteak Tomatoes", "flipp", [2]),
    (103, "Butternut Squash", "seed", [3]),
    (104, "Prosciutto di Parma 100g", "flipp", [4]),
    (105, "Marzipan Bar", "flipp", [5]),
    (106, "NATREL MILK 2L", "flipp", [6]),
    (107, "Firm Tofu", "flipp", [7]),          # proposed, not confirmed
    (108, "Green Lentils", "flipp", [8]),      # rejected
    (109, "Mystery Item", "seed", []),          # unlinked, even from seed
    (110, "Chicken or Tomatoes", "flipp", [9, 2]),  # tile: one violating product
    (111, "BILLY BEE HONEY 500G", "flipp", [10]),
    (112, "Smucker's Strawberry Jam", "flipp", [11]),  # by-product undecided
]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE products (
            product_id INTEGER PRIMARY KEY, canonical_name TEXT, status TEXT,
            contains_meat BOOLEAN, contains_fish BOOLEAN, contains_dairy BOOLEAN,
            contains_egg BOOLEAN, contains_nuts BOOLEAN,
            contains_animal_byproduct BOOLEAN);
        CREATE TABLE deals (deal_id INTEGER PRIMARY KEY, product_name TEXT,
                            source TEXT);
        CREATE TABLE deal_products (deal_id INTEGER, product_id INTEGER);
        """
    )
    c.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?)", PRODUCTS)
    for deal_id, name, source, pids in DEALS:
        c.execute("INSERT INTO deals VALUES (?,?,?)", (deal_id, name, source))
        for pid in pids:
            c.execute("INSERT INTO deal_products VALUES (?,?)", (deal_id, pid))
    yield c
    c.close()


def served(conn, restrictions):
    fragment, params = deal_gate.deal_filter(restrictions)
    assert params == {}
    rows = conn.execute(
        "SELECT d.product_name FROM deals d WHERE 1=1" + fragment
        + " ORDER BY d.deal_id"
    ).fetchall()
    return [r[0] for r in rows]


# --- the owner's intent -----------------------------------------------------

def test_peanut_butter_reaches_a_vegan(conn):
    assert "KRAFT PEANUT BUTTER 1KG" in served(conn, ["vegan"])


def test_beefsteak_tomatoes_reach_a_vegetarian(conn):
    assert "Beefsteak Tomatoes" in served(conn, ["vegetarian"])


def test_butternut_squash_reaches_a_nut_free_user(conn):
    assert "Butternut Squash" in served(conn, ["no_nuts"])
    assert "Butternut Squash" in served(conn, ["nut_free"])


# --- attributes withhold what names would miss ------------------------------

def test_prosciutto_is_withheld_from_a_vegetarian(conn):
    assert "Prosciutto di Parma 100g" not in served(conn, ["vegetarian"])


def test_marzipan_is_withheld_from_a_nut_free_user(conn):
    assert "Marzipan Bar" not in served(conn, ["no_nuts"])


def test_peanut_butter_is_withheld_from_a_nut_free_user(conn):
    assert "KRAFT PEANUT BUTTER 1KG" not in served(conn, ["no_nuts"])


def test_dairy_free_withholds_milk(conn):
    names = served(conn, ["dairy_free"])
    assert "NATREL MILK 2L" not in names
    assert "Beefsteak Tomatoes" in names


def test_vegan_sees_exactly_the_confirmed_plant_products(conn):
    assert served(conn, ["vegan"]) == [
        "KRAFT PEANUT BUTTER 1KG", "Beefsteak Tomatoes", "Butternut Squash",
        "Marzipan Bar",
    ]


# --- contains_animal_byproduct (migration 015) ------------------------------

def test_honey_is_withheld_from_a_vegan_and_served_to_a_vegetarian(conn):
    assert "BILLY BEE HONEY 500G" not in served(conn, ["vegan"])
    for restriction in ("vegetarian", "pescatarian", "no_nuts", "dairy_free"):
        assert "BILLY BEE HONEY 500G" in served(conn, [restriction]), restriction


def test_undecided_byproduct_is_withheld_from_a_vegan(conn):
    """NULL must not read as false. A bare ``p.contains_animal_byproduct`` in
    the OR chain evaluates to NULL, NOT EXISTS sees no violating row, and the
    deal is served -- fail OPEN. This runs the real fragment through SQLite's
    three-valued logic to prove it does not."""
    assert "Smucker's Strawberry Jam" not in served(conn, ["vegan"])


def test_undecided_byproduct_does_not_affect_other_restrictions(conn):
    for restriction in ("vegetarian", "pescatarian", "no_nuts", "dairy_free"):
        assert "Smucker's Strawberry Jam" in served(conn, [restriction]), restriction


def test_undecided_byproduct_does_not_unblock_a_product_failing_otherwise(conn):
    """Chicken with a NULL by-product still fails vegetarian on meat."""
    assert "Chicken or Tomatoes" not in served(conn, ["vegetarian"])
    assert "NATREL MILK 2L" in served(conn, ["vegetarian"])


def test_gate_scope_changes_for_vegans_only():
    assert "contains_animal_byproduct" in deal_gate.gate_scope(["vegan"])
    for label in ("vegetarian", "pescatarian", "no_nuts", "dairy_free"):
        assert "contains_animal_byproduct" not in deal_gate.gate_scope([label])


# --- fail closed ------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "Firm Tofu",        # proposed
    "Green Lentils",    # rejected
    "Mystery Item",     # unlinked, and from a hand-curated source
])
def test_anything_not_confirmed_is_withheld(conn, name):
    assert name not in served(conn, ["vegetarian"])


def test_a_tile_is_withheld_if_any_product_on_it_violates(conn):
    assert "Chicken or Tomatoes" not in served(conn, ["vegetarian"])
    # Pescatarian forbids meat too; nut-free does not care about chicken.
    assert "Chicken or Tomatoes" in served(conn, ["no_nuts"])


def test_an_unrecognised_restriction_still_gates(conn):
    """Unknown label: no attribute to enforce, but still no unconfirmed deal."""
    names = served(conn, ["halal"])
    assert "Mystery Item" not in names
    assert "Firm Tofu" not in names
    assert "Beefsteak Tomatoes" in names


def test_source_plays_no_part(conn):
    """Seed and Flipp deals are judged identically, by their products."""
    fragment, _ = deal_gate.deal_filter(["vegan"])
    assert "source" not in fragment
    names = served(conn, ["vegan"])
    assert "Beefsteak Tomatoes" in names     # flipp
    assert "Butternut Squash" in names       # seed
    assert "Mystery Item" not in names       # seed, unlinked


# --- unrestricted users -----------------------------------------------------

@pytest.mark.parametrize("restrictions", [None, [], ["", "  "]])
def test_unrestricted_user_sees_every_deal(conn, restrictions):
    assert deal_gate.deal_filter(restrictions) == ("", {})
    assert len(served(conn, restrictions)) == len(DEALS)


# --- module surface ---------------------------------------------------------

def test_restrictions_are_active():
    assert deal_gate.restrictions_are_active(["vegan"]) is True
    assert deal_gate.restrictions_are_active([]) is False
    assert deal_gate.restrictions_are_active(None) is False
    assert deal_gate.restrictions_are_active(["", "  "]) is False


def test_gate_scope_separates_gated_from_open():
    assert deal_gate.gate_scope(None) == "open"
    assert deal_gate.gate_scope([]) == "open"
    assert deal_gate.gate_scope(["vegan"]) != "open"
    assert deal_gate.gate_scope(["halal"]) != "open"
    # Labels with the same attributes share a scope, so they share a cache.
    assert deal_gate.gate_scope(["no_nuts"]) == deal_gate.gate_scope(["nut_free"])
    assert deal_gate.gate_scope(["vegan"]) != deal_gate.gate_scope(["vegetarian"])


def test_the_gate_reads_no_setting():
    """No flag can serve unvetted deals to a restricted user: the gate's
    public surface is exactly these functions, none of which consult config."""
    assert set(deal_gate.__all__) == {
        "deal_filter", "gate_scope", "restrictions_are_active",
    }
    assert not hasattr(deal_gate, "settings")
    assert settings.deal_ingestion_enabled is False


def test_deal_filter_is_the_products_gate():
    for r in (["vegan"], ["vegetarian", "no_nuts"], ["halal"]):
        assert deal_gate.deal_filter(r) == products.compliant_product_filter(r)


# --------------------------------------------------------------------------
# The two read paths apply the gate in SQL
# --------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, query, params=None):
        self.sink.append((query, params))

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDB:
    def __init__(self):
        self.calls = []

    def get_cursor(self):
        return _FakeCursor(self.calls)


def test_fetch_current_deals_gates_a_restricted_user_before_the_limit():
    from app.services.database import DatabaseService

    svc = DatabaseService()
    svc.db = _FakeDB()
    svc.fetch_current_deals("M5V3A8", dietary_restrictions=["vegan"])
    query, _ = svc.db.calls[0]
    assert "deal_products" in query
    assert "d.source = ANY" not in query
    assert query.index("deal_products") < query.index("LIMIT")


def test_fetch_current_deals_is_open_for_an_unrestricted_user():
    from app.services.database import DatabaseService

    svc = DatabaseService()
    svc.db = _FakeDB()
    svc.fetch_current_deals("M5V3A8", dietary_restrictions=[])
    query, _ = svc.db.calls[0]
    assert "deal_products" not in query


def test_store_service_gates_and_keys_the_cache_by_scope(monkeypatch):
    from app.services import store_service

    fake = _FakeDB()
    keys = []
    monkeypatch.setattr(store_service, "db", fake)
    monkeypatch.setattr(store_service.cache, "get", lambda k: keys.append(k))
    monkeypatch.setattr(store_service.cache, "set", lambda *a, **kw: None)

    store_service.StoreService.get_current_deals_by_postal_code(
        "M5V3A8", dietary_restrictions=["vegetarian"]
    )
    store_service.StoreService.get_current_deals_by_postal_code("M5V3A8")

    gated_query, open_query = fake.calls[0][0], fake.calls[1][0]
    assert "deal_products" in gated_query
    assert "deal_products" not in open_query
    assert keys[0] != keys[1]
    assert keys[1].endswith(":open")
