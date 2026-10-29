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
#:  animal_byproduct, gluten). The last two are nullable (migrations 015,
#:  017); None is a product confirmed before the column existed and never
#:  revisited.
PRODUCTS = [
    (1, "peanut butter", "confirmed", 0, 0, 0, 0, 1, 0, None),
    (2, "beefsteak tomatoes", "confirmed", 0, 0, 0, 0, 0, 0, None),
    (3, "butternut squash", "confirmed", 0, 0, 0, 0, 0, 0, None),
    (4, "prosciutto", "confirmed", 1, 0, 0, 0, 0, None, None),
    (5, "marzipan", "confirmed", 0, 0, 0, 0, 1, 0, None),
    (6, "milk", "confirmed", 0, 0, 1, 0, 0, None, None),
    (7, "tofu", "proposed", 0, 0, 0, 0, 0, 0, None),
    (8, "lentils", "rejected", 0, 0, 0, 0, 0, 0, None),
    (9, "chicken breast", "confirmed", 1, 0, 0, 0, 0, None, None),
    (10, "honey", "confirmed", 0, 0, 0, 0, 0, 1, None),
    (11, "strawberry jam", "confirmed", 0, 0, 0, 0, 0, None, None),
    (12, "white bread", "confirmed", 0, 0, 0, 0, 0, 0, 1),
    (13, "basmati rice", "confirmed", 0, 0, 0, 0, 0, 0, 0),
    (14, "granola", "confirmed", 0, 0, 0, 0, 0, 0, None),  # gluten undecided
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
    (113, "Wonder White Bread", "flipp", [12]),         # contains gluten
    (114, "Basmati Rice 2kg", "flipp", [13]),           # explicitly gluten-free
    (115, "Granola Clusters", "flipp", [14]),           # gluten undecided
    (116, "Bread or Rice", "flipp", [12, 13]),          # tile: one gluten product
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
            contains_animal_byproduct BOOLEAN, contains_gluten BOOLEAN);
        CREATE TABLE deals (deal_id INTEGER PRIMARY KEY, product_name TEXT,
                            source TEXT);
        CREATE TABLE deal_products (deal_id INTEGER, product_id INTEGER);
        """
    )
    c.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?)", PRODUCTS)
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
        "Marzipan Bar", "Wonder White Bread", "Basmati Rice 2kg",
        "Granola Clusters", "Bread or Rice",
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


# --- contains_gluten (migration 017) ----------------------------------------

def test_gluten_free_user_is_served_only_explicit_false_gluten(conn):
    """Everything else on the menu has NULL gluten, so it is withheld: NULL
    fails closed, and the real fragment is run through SQLite's three-valued
    logic to prove a bare NULL does not fail open."""
    assert served(conn, ["gluten_free"]) == ["Basmati Rice 2kg"]


def test_gluten_free_withholds_gluten_and_undecided_gluten(conn):
    names = served(conn, ["gluten_free"])
    assert "Wonder White Bread" not in names   # explicitly true
    assert "Granola Clusters" not in names     # NULL
    assert "Bread or Rice" not in names        # tile: one violating product
    assert "Butternut Squash" not in names     # NULL on a confirmed product


def test_gluten_does_not_affect_other_restrictions(conn):
    for restriction in ("vegetarian", "pescatarian", "no_nuts", "dairy_free"):
        names = served(conn, [restriction])
        for bread in ("Wonder White Bread", "Granola Clusters", "Bread or Rice"):
            assert bread in names, (restriction, bread)


def test_gate_scope_names_gluten_for_gluten_free_only():
    assert "contains_gluten" in deal_gate.gate_scope(["gluten_free"])
    for label in ("vegan", "vegetarian", "pescatarian", "no_nuts", "dairy_free"):
        assert "contains_gluten" not in deal_gate.gate_scope([label])


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


# --------------------------------------------------------------------------
# not_kosher (migration 021): the gate is per product
# --------------------------------------------------------------------------

#: (product_id, canonical_name, status, meat, dairy, fish, not_kosher)
KOSHER_PRODUCTS = [
    (201, "kosher beef", "confirmed", 1, 0, 0, 0),
    (202, "kosher cheddar", "confirmed", 0, 1, 0, 0),
    (203, "uncertified brie", "confirmed", 0, 1, 0, 1),
    (204, "pasta", "confirmed", 0, 0, 0, None),          # undecided
    (205, "salmon", "confirmed", 0, 0, 1, 0),
    (206, "shrimp", "confirmed", 0, 0, 1, 1),
    (207, "kosher tofu", "proposed", 0, 0, 0, 0),         # not confirmed
]

#: (deal_id, product_name, [linked product ids])
KOSHER_DEALS = [
    (301, "KOSHER GROUND BEEF", [201]),
    (302, "KOSHER CHEDDAR", [202]),
    (303, "BRIE", [203]),
    (304, "PASTA", [204]),
    (305, "SALMON", [205]),
    (306, "SHRIMP", [206]),
    (307, "TOFU", [207]),
    (308, "UNLINKED", []),
    # A tile linking a kosher meat and a kosher dairy product. Serving it is
    # fine -- each product clears; "no meat with dairy" is a recipe rule.
    (309, "BEEF OR CHEDDAR", [201, 202]),
    (310, "CHEDDAR OR BRIE", [202, 203]),
]


def _kosher_rows():
    out = []
    for pid, name, status, meat, dairy, fish, nk in KOSHER_PRODUCTS:
        out.append({"product_id": pid, "canonical_name": name, "status": status,
                    "contains_meat": bool(meat), "contains_fish": bool(fish),
                    "contains_dairy": bool(dairy), "contains_egg": False,
                    "contains_nuts": False, "contains_animal_byproduct": False,
                    "contains_gluten": False,
                    "not_kosher": None if nk is None else bool(nk)})
    return {r["product_id"]: r for r in out}


@pytest.fixture
def kconn():
    c = sqlite3.connect(":memory:")
    cols = ", ".join(f"{a} BOOLEAN" for a in products.ATTRIBUTES)
    c.executescript(
        f"""
        CREATE TABLE products (product_id INTEGER PRIMARY KEY,
                               canonical_name TEXT, status TEXT, {cols});
        CREATE TABLE deals (deal_id INTEGER PRIMARY KEY, product_name TEXT,
                            source TEXT);
        CREATE TABLE deal_products (deal_id INTEGER, product_id INTEGER);
        """
    )
    for row in _kosher_rows().values():
        keys = ["product_id", "canonical_name", "status", *products.ATTRIBUTES]
        c.execute(f"INSERT INTO products ({', '.join(keys)}) VALUES "
                  f"({', '.join('?' for _ in keys)})", [row[k] for k in keys])
    for deal_id, name, pids in KOSHER_DEALS:
        c.execute("INSERT INTO deals VALUES (?,?,?)", (deal_id, name, "flipp"))
        for pid in pids:
            c.execute("INSERT INTO deal_products VALUES (?,?)", (deal_id, pid))
    yield c
    c.close()


def test_kosher_user_is_served_only_explicit_false_not_kosher(kconn):
    assert served(kconn, ["kosher"]) == [
        "KOSHER GROUND BEEF", "KOSHER CHEDDAR", "SALMON", "BEEF OR CHEDDAR"]


@pytest.mark.parametrize("name", ["BRIE", "SHRIMP", "PASTA", "TOFU", "UNLINKED",
                                  "CHEDDAR OR BRIE"])
def test_kosher_withholds_true_undecided_unconfirmed_and_unlinked(kconn, name):
    assert name not in served(kconn, ["kosher"])


def test_not_kosher_does_not_affect_other_restrictions(kconn):
    assert "PASTA" in served(kconn, ["vegan"])
    assert "BRIE" in served(kconn, ["vegetarian"])
    assert "SHRIMP" in served(kconn, ["no_nuts"])


def test_null_not_kosher_does_not_fail_open_in_sql(kconn):
    """``p.not_kosher`` bare would read NULL as clean; IS NOT FALSE does not."""
    fragment, _ = deal_gate.deal_filter(["kosher"])
    assert "p.not_kosher IS NOT FALSE" in fragment
    assert "PASTA" not in served(kconn, ["kosher"])


def test_gate_scope_names_not_kosher_for_kosher_only():
    assert "not_kosher" in deal_gate.gate_scope(["kosher"])
    for label in ("vegan", "vegetarian", "pescatarian", "no_nuts", "dairy_free",
                  "gluten_free"):
        assert "not_kosher" not in deal_gate.gate_scope([label])


@pytest.mark.parametrize("restrictions", [
    ["kosher"], ["kosher", "vegetarian"], ["kosher", "dairy_free"], ["vegan"],
    ["pescatarian"], ["halal"], [],
])
def test_sql_gate_and_deal_allows_agree_on_every_kosher_deal(kconn, restrictions):
    rows = _kosher_rows()
    expected = [name for _, name, pids in KOSHER_DEALS
                if products.deal_allows([rows[p] for p in pids], restrictions)]
    assert served(kconn, restrictions) == expected
