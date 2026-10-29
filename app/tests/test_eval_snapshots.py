"""Frozen snapshots (ROADMAP Track E §2): loader, fakes, capture script, and
the real compiled graph run against every committed snapshot.

Deterministic and offline: no network, no LLM, no database. The capture
script is exercised against a fake cursor; the graph runs use scripted chat
models that answer from what the prompt actually offered, so they work on any
snapshot without a per-snapshot script.
"""
import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from app import main_recipe_generation
from app.agents.dietary import CitationCatalog, compliance_report
from app.eval import snapshots as snap_mod
from app.eval.snapshots import (
    FORMAT_VERSION,
    Snapshot,
    SnapshotError,
    dump_snapshot,
    load_snapshot,
    snapshot_path,
)
from app.services.products import ATTRIBUTES, restrictions_to_attributes
from app.tests.test_graph_offline import RecordingTracker, ScriptedChatModel
from scripts import capture_snapshot as capture_mod

COMMITTED = ("seed27", "week-2026-10-16", "sparse5")
POSTAL = "M5V3A8"


@pytest.fixture(autouse=True)
def no_database_no_mlflow(monkeypatch):
    """Anything that reaches Postgres or MLflow fails the test."""
    from app.db.database import Database
    from app.services import mlflow_logger

    def no_db(*args, **kwargs):
        raise AssertionError("snapshot test touched the database")

    def no_mlflow():
        raise AssertionError("snapshot test touched MLflow")

    monkeypatch.setattr(Database, "initialize", no_db)
    monkeypatch.setattr(Database, "get_connection", no_db)
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", no_mlflow)


# --------------------------------------------------------------------------
# A minimal hand-built snapshot
# --------------------------------------------------------------------------

def _product(pid, name, status="confirmed", on_hand=False, **attrs):
    row = {"product_id": pid, "canonical_name": name, "category": "x",
           "status": status, "assumed_on_hand": on_hand, "merged_into": None}
    row.update({a: False for a in ATTRIBUTES})
    row.update(attrs)
    return row


def _deal(did, name, price, disc=None, store=1, valid=("2026-10-15", "2026-10-22")):
    return {"deal_id": did, "store_id": store, "product_name": name, "brand": None,
            "sale_price": price, "regular_price": None, "discount_percentage": disc,
            "unit": None, "category": None, "valid_from": valid[0], "valid_until": valid[1],
            "source": "flipp", "product_id": None, "sale_story": None, "pre_price_text": None}


def _link(did, pid, pos=0):
    return {"deal_id": did, "product_id": pid, "tile_position": pos, "price_is_per_item": False}


def mini() -> dict:
    return {
        "meta": {"name": "mini", "format_version": FORMAT_VERSION,
                 "captured_at": "2026-10-29T00:00:00+00:00", "source": "test",
                 "as_of": "2026-10-16", "postal_codes": [POSTAL],
                 "schema_migration": None, "notes": None},
        "stores": [{"store_id": 1, "name": "Store A", "chain": "a", "postal_code": POSTAL,
                    "city": None, "province": None}],
        "deals": [
            _deal(1, "TOFU", 2.0, disc=10),
            # Name says nothing about meat; the attribute does.
            _deal(2, "GARDEN MEDLEY", 3.0, disc=30),
            _deal(3, "UNLINKED THING", 1.0),
            _deal(4, "HONEY", 4.0, disc=10),
            _deal(5, "TILE: MILK + HAM", 5.0),
        ],
        "deal_products": [_link(1, 10), _link(2, 20), _link(4, 40),
                          _link(5, 51, 1), _link(5, 50, 0)],
        "products": [
            _product(10, "Tofu"),
            _product(20, "Garden Medley", contains_meat=True),
            _product(40, "Honey", contains_animal_byproduct=None),
            _product(50, "Milk", contains_dairy=True),
            _product(51, "Ham", contains_meat=True),
            _product(90, "Salt", on_hand=True),
            _product(91, "Proposed Staple", status="proposed", on_hand=True),
        ],
    }


# --------------------------------------------------------------------------
# Loader validation
# --------------------------------------------------------------------------

def test_mini_snapshot_loads():
    s = Snapshot.from_dict(mini())
    assert s.name == "mini" and s.as_of == dt.date(2026, 10, 16)
    assert s.postal_codes == [POSTAL]


@pytest.mark.parametrize("name", COMMITTED)
def test_committed_snapshots_load_and_describe_themselves(name):
    s = load_snapshot(name)
    meta = s.meta
    assert meta["name"] == name
    assert meta["format_version"] == FORMAT_VERSION
    assert meta["postal_codes"] == [POSTAL]
    dt.date.fromisoformat(meta["as_of"])
    dt.datetime.fromisoformat(meta["captured_at"])
    assert meta["source"] and meta["notes"]


def test_committed_snapshot_contents():
    seed = load_snapshot("seed27")
    assert len(seed.data["deals"]) == 27
    assert {d["source"] for d in seed.data["deals"]} == {"seed"}
    assert seed.as_of == dt.date(2026, 7, 15)

    week = load_snapshot("week-2026-10-16")
    assert week.as_of == dt.date(2026, 10, 16)
    assert len(week.data["deals"]) > 100

    sparse = load_snapshot("sparse5")
    assert len(sparse.data["deals"]) == 5


@pytest.mark.parametrize("version", [0, 1, 3, "2", None, True])
def test_unknown_format_version_is_refused(version):
    data = mini()
    data["meta"]["format_version"] = version
    with pytest.raises(SnapshotError, match="format_version"):
        Snapshot.from_dict(data)


def test_a_v1_snapshot_is_refused_with_the_not_kosher_upgrade_path():
    """v1 predates not_kosher. It is refused rather than read with a silent
    default, and the error says how to upgrade it honestly (null)."""
    data = mini()
    data["meta"]["format_version"] = 1
    for p in data["products"]:
        p.pop("not_kosher", None)
    with pytest.raises(SnapshotError, match="not_kosher"):
        Snapshot.from_dict(data)


def test_committed_snapshots_carry_not_kosher_as_undecided():
    """Migrated v1 -> v2 with null: no product had a kosher decision when
    they were captured, and null fails closed for kosher users."""
    for name in ("seed27", "week-2026-10-16", "sparse5"):
        snap = load_snapshot(name)
        assert snap.meta["format_version"] == 2
        assert {p["not_kosher"] for p in snap.data["products"]} == {None}
        assert snap.deal_source().fetch_current_deals(snap.postal_codes[0], ["kosher"]) == []


def test_missing_version_is_refused():
    data = mini()
    del data["meta"]["format_version"]
    with pytest.raises(SnapshotError, match="format_version"):
        Snapshot.from_dict(data)


@pytest.mark.parametrize("section,field", [
    ("meta", "as_of"), ("meta", "captured_at"), ("meta", "source"), ("meta", "name"),
    ("stores", "postal_code"), ("deals", "valid_until"), ("deals", "sale_price"),
    ("deal_products", "product_id"), ("products", "status"),
    *[("products", a) for a in ATTRIBUTES],
])
def test_missing_field_is_refused(section, field):
    data = mini()
    target = data[section] if section == "meta" else data[section][0]
    del target[field]
    with pytest.raises(SnapshotError, match=f"missing field.*{field}"):
        Snapshot.from_dict(data)


@pytest.mark.parametrize("section", ["meta", "stores", "deals", "deal_products", "products"])
def test_missing_section_is_refused(section):
    data = mini()
    del data[section]
    with pytest.raises(SnapshotError, match="missing section"):
        Snapshot.from_dict(data)


def test_unexpected_field_and_section_are_refused():
    data = mini()
    data["products"][0]["is_vegan"] = True
    with pytest.raises(SnapshotError, match="unexpected field"):
        Snapshot.from_dict(data)
    data = mini()
    data["users"] = []
    with pytest.raises(SnapshotError, match="unexpected section"):
        Snapshot.from_dict(data)


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["products"][0].update(contains_meat="no"), "contains_meat"),
    (lambda d: d["products"][0].update(contains_meat=0), "contains_meat"),
    (lambda d: d["deals"][0].update(sale_price=True), "sale_price"),
    (lambda d: d["deals"][0].update(sale_price="2.00"), "sale_price"),
    (lambda d: d["deals"][0].update(deal_id=None), "must not be null"),
    (lambda d: d["products"][0].update(status=None), "must not be null"),
    (lambda d: d["deals"].append(copy.deepcopy(d["deals"][0])), "duplicate"),
    (lambda d: d["deals"][0].update(store_id=99), "store 99 not in stores"),
    (lambda d: d["deal_products"].append(_link(1, 999)), "product not in products"),
    (lambda d: d["deal_products"].append(_link(999, 10)), "deal not in deals"),
    (lambda d: d["deals"][0].update(valid_until="2026-10-15"), "not live at as_of"),
    (lambda d: d["deals"][0].update(valid_from="2026-10-17"), "not live at as_of"),
    (lambda d: d["meta"].update(as_of="16/10/2026"), "ISO date"),
    (lambda d: d["meta"].update(postal_codes=[]), "postal_codes"),
    (lambda d: d["stores"][0].update(postal_code="H2X1Y4"), "not in meta.postal_codes"),
])
def test_malformed_snapshots_are_refused(mutate, match):
    data = mini()
    mutate(data)
    with pytest.raises(SnapshotError, match=match):
        Snapshot.from_dict(data)


def test_bad_json_file_is_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SnapshotError, match="not valid JSON"):
        load_snapshot(bad)


def test_null_attributes_stay_null_and_fail_closed():
    s = Snapshot.from_dict(mini())
    honey = next(p for p in s.data["products"] if p["product_id"] == 40)
    assert honey["contains_animal_byproduct"] is None
    # Through the catalog, still None -- never defaulted.
    rows = s.catalog().fetch_deal_products([4])
    assert rows["4"][0]["contains_animal_byproduct"] is None
    # And the gate fails closed on it for a vegan, but not for a vegetarian.
    deals = s.deal_source()
    assert 4 not in {d["deal_id"] for d in deals.fetch_current_deals(POSTAL, ["vegan"])}
    assert 4 in {d["deal_id"] for d in deals.fetch_current_deals(POSTAL, ["vegetarian"])}


# --------------------------------------------------------------------------
# Canonical form: determinism and round trip
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", COMMITTED)
def test_committed_files_are_canonical(name):
    path = snapshot_path(name)
    text = path.read_text(encoding="utf-8")
    assert dump_snapshot(json.loads(text)) == text
    assert dump_snapshot(load_snapshot(path).data) == text


def test_dump_is_order_independent(tmp_path):
    data = mini()
    shuffled = copy.deepcopy(data)
    for section in ("stores", "deals", "deal_products", "products"):
        shuffled[section].reverse()
    shuffled["meta"] = dict(reversed(list(shuffled["meta"].items())))
    assert dump_snapshot(data) == dump_snapshot(shuffled)
    out = snap_mod.write_snapshot(shuffled, tmp_path / "x.json")
    assert out.read_bytes() == dump_snapshot(data).encode("utf-8")
    assert load_snapshot(out).data == Snapshot.from_dict(data).data


# --------------------------------------------------------------------------
# The fakes mirror DatabaseService / recipe_catalog
# --------------------------------------------------------------------------

def test_deal_source_gates_on_attributes_orders_and_shapes_like_production():
    deals = Snapshot.from_dict(mini()).deal_source()

    open_ = deals.fetch_current_deals(POSTAL)
    # discount DESC NULLS LAST, then sale_price ASC, then deal_id.
    assert [d["deal_id"] for d in open_] == [2, 1, 4, 3, 5]
    assert list(open_[0]) == list(snap_mod.SERVED_DEAL_KEYS)
    assert open_[0]["store_name"] == "Store A" and open_[0]["chain"] == "a"

    veg = [d["deal_id"] for d in deals.fetch_current_deals(POSTAL, ["vegetarian"])]
    # 2: contains_meat (despite the name); 3: unlinked; 5: one of its two
    # products is ham -- every linked product must clear.
    assert veg == [1, 4]
    # Unknown label: still behind the gate (linked + confirmed), no attribute.
    assert [d["deal_id"] for d in deals.fetch_current_deals(POSTAL, ["halal"])] == [2, 1, 4, 5]
    assert deals.calls[0] == (POSTAL, None)


def test_deal_source_unconfirmed_product_withholds():
    data = mini()
    data["products"][0]["status"] = "proposed"  # Tofu
    deals = Snapshot.from_dict(data).deal_source()
    assert 1 not in {d["deal_id"] for d in deals.fetch_current_deals(POSTAL, ["vegan"])}
    assert 1 in {d["deal_id"] for d in deals.fetch_current_deals(POSTAL, [])}


def test_deal_source_caps_at_the_production_limit():
    data = mini()
    data["deals"] = [_deal(i, f"D{i}", 1.0) for i in range(1, 251)]
    data["deal_products"] = []
    out = Snapshot.from_dict(data).deal_source().fetch_current_deals(POSTAL)
    assert len(out) == snap_mod.DEAL_LIMIT


def test_deal_source_refuses_an_uncovered_postal_code():
    with pytest.raises(SnapshotError, match="postal code"):
        Snapshot.from_dict(mini()).deal_source().fetch_current_deals("H2X1Y4", [])


def test_catalog_mirrors_recipe_catalog():
    cat = Snapshot.from_dict(mini()).catalog()
    staples = cat.fetch_pantry_staples()
    assert [s["product_id"] for s in staples] == [90]  # proposed staple excluded
    assert list(staples[0]) == list(snap_mod.CATALOG_PRODUCT_KEYS)

    linked = cat.fetch_deal_products([5, "1", None, "x", 3])
    assert set(linked) == {"1", "5"}  # 3 unlinked -> absent
    assert [p["product_id"] for p in linked["5"]] == [50, 51]  # tile_position order
    assert cat.fetch_deal_products([]) == {}


# --------------------------------------------------------------------------
# Capture script, against a fake cursor
# --------------------------------------------------------------------------

class _Col:
    def __init__(self, name):
        self.name = name


class FakeCursor:
    """Answers the capture's SELECTs from ``mini()``'s rows; records SQL."""

    def __init__(self, data, read_only="on"):
        self.data = data
        self.read_only = read_only
        self.executed = []
        self._rows = []
        self.description = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split())
        d = self.data
        if s.startswith("SHOW transaction_read_only"):
            rows = [{"transaction_read_only": self.read_only}]
        elif "transaction_timestamp()" in s:
            rows = [{"captured_at": dt.datetime(2026, 10, 29, 12, 0, 0)}]
        elif "schema_migrations" in s:
            rows = [{"latest": "020_ingestion_runs.sql"}]
        elif "FROM deals d" in s:
            rows = [dict(r) for r in d["deals"]]
        elif "FROM stores s" in s:
            rows = [r for r in d["stores"] if r["store_id"] in params["store_ids"]]
        elif "FROM deal_products dp" in s:
            rows = [r for r in d["deal_products"] if r["deal_id"] in params["deal_ids"]]
        elif "FROM products p" in s:
            rows = [r for r in d["products"]
                    if r["product_id"] in params["product_ids"] or r["assumed_on_hand"]]
        else:
            raise AssertionError(f"unexpected SQL {s[:80]}")
        self._rows = rows
        self.description = [_Col(k) for k in (rows[0] if rows else {})]

    def fetchall(self):
        return [tuple(r.values()) for r in self._rows]


def test_capture_issues_only_reads_and_produces_a_valid_snapshot():
    cur = FakeCursor(mini())
    snap = capture_mod.capture(cur, name="mini", as_of=dt.date(2026, 10, 16),
                               postal_codes=[POSTAL], source="test")
    heads = {" ".join(sql.split()).split()[0] for sql, _ in cur.executed}
    assert heads <= {"SELECT", "SHOW"}
    assert cur.executed[0][0] == "SHOW transaction_read_only"
    # No user rows read.
    assert not any("users" in sql for sql, _ in cur.executed)
    deal_params = next(p for sql, p in cur.executed if "FROM deals d" in sql)
    assert deal_params == {"postal_codes": [POSTAL], "as_of": dt.date(2026, 10, 16)}

    loaded = Snapshot.from_dict(snap)
    assert loaded.meta["captured_at"] == "2026-10-29T12:00:00+00:00"
    assert loaded.meta["schema_migration"] == "020_ingestion_runs.sql"
    expected = Snapshot.from_dict(mini()).data
    for section in ("stores", "deals", "deal_products", "products"):
        assert loaded.data[section] == expected[section]


def test_capture_refuses_a_writable_transaction():
    cur = FakeCursor(mini(), read_only="off")
    with pytest.raises(capture_mod.NotReadOnly, match="not read-only"):
        capture_mod.capture(cur, name="m", as_of=dt.date(2026, 10, 16),
                            postal_codes=[POSTAL], source="t")
    assert len(cur.executed) == 1  # stopped before reading any data


@pytest.mark.parametrize("sql", [
    "INSERT INTO deals VALUES (1)", "UPDATE products SET status='x'",
    "DELETE FROM deals", "  with x as (delete from deals returning *) select 1",
    "SELECT 1; DROP TABLE deals", "SET default_transaction_read_only = off", "",
])
def test_read_guard_refuses_non_reads(sql):
    cur = FakeCursor(mini())
    with pytest.raises(capture_mod.NotReadOnly):
        capture_mod._read(cur, sql)
    assert cur.executed == []


def test_capture_sql_is_select_only():
    for sql in (capture_mod.DEALS_SQL, capture_mod.STORES_SQL, capture_mod.LINKS_SQL,
                capture_mod.PRODUCTS_SQL, capture_mod.CLOCK_SQL, capture_mod.MIGRATION_SQL):
        assert sql.lstrip().upper().startswith("SELECT")
        assert ";" not in sql


# --------------------------------------------------------------------------
# The real compiled graph against every committed snapshot
# --------------------------------------------------------------------------

def _json_after(prompt: str, header: str):
    """The JSON value printed right after ``header`` in a prompt."""
    start = prompt.index(header) + len(header)
    start = min(i for i in (prompt.find("[", start), prompt.find("{", start)) if i >= 0)
    value, _ = json.JSONDecoder().raw_decode(prompt, start)
    return value


def _chef_reply(prompt: str) -> str:
    """Three groups from the deals the prompt actually shows."""
    offered = _json_after(prompt, "**Available Deals:**")
    picks = offered[:6]
    assert picks, "Chef was shown no deals"
    groups = [[{"product_name": d["product_name"], "quantity_estimate": "1",
                "deal_id": d["deal_id"]} for d in picks[i::3]] or
              [{"product_name": picks[0]["product_name"], "quantity_estimate": "1",
                "deal_id": picks[0]["deal_id"]}]
              for i in range(3)]
    return json.dumps({"ingredient_groups": groups, "ingredient_reuse_map": {},
                       "rationale": "scripted"})


def _sous_chef_reply(prompt: str) -> str:
    """One recipe citing every assigned deal plus the first offered staple."""
    deals = _json_after(prompt, "**Your Assigned Deals (cite each by its deal_id):**")
    staples = _json_after(prompt, "**Pantry Staples Already On Hand (cite each by its product_id):**")
    ingredients = [{"name": d.get("product_name") or "deal", "deal_id": d["deal_id"],
                    "quantity": "1", "unit": "unit"} for d in deals]
    if staples:
        ingredients.append({"name": staples[0]["name"],
                            "product_id": staples[0]["product_id"],
                            "quantity": "1", "unit": "tsp"})
    recipe = {"name": "Scripted " + "+".join(str(d["deal_id"]) for d in deals),
              "meal_type": "dinner", "cuisine_type": "Any", "servings": 2,
              "estimated_prep_time": "20 minutes", "ingredients": ingredients,
              "instructions": ["Combine " + ", ".join(
                  f"{{{i}}}" for i in range(len(ingredients))) + ".", "Cook it."]}
    return json.dumps({"recipes": [recipe]})


NUTRITIONIST = json.dumps({"approved": False, "feedback": "advisory only",
                           "nutrition_facts": {"calories_per_serving": 500},
                           "health_score": "70"})


def _deps(snapshot):
    return snapshot.graph_deps(
        chef_llm=ScriptedChatModel(respond=_chef_reply, prompts=[]),
        sous_chef_llm=ScriptedChatModel(respond=_sous_chef_reply, prompts=[]),
        nutritionist_llm=ScriptedChatModel(respond=lambda p: NUTRITIONIST, prompts=[]),
        tracker=RecordingTracker(),
    )


def _run(deps, restrictions):
    return main_recipe_generation.run_recipe_generation(
        user_id=1, postal_code=POSTAL, budget=80.0, household_size=2,
        dietary_restrictions=restrictions, num_meals=3,
        budget_period="week", budget_spent_this_period=0.0, graph_deps=deps)


def _clears(snapshot, deal_id, restrictions) -> bool:
    """Ground truth from the snapshot rows, written out longhand: linked,
    every linked product confirmed, every required attribute exactly False."""
    required = restrictions_to_attributes(restrictions)
    linked = [l["product_id"] for l in snapshot.data["deal_products"] if l["deal_id"] == deal_id]
    products = {p["product_id"]: p for p in snapshot.data["products"]}
    return bool(linked) and all(
        products[pid]["status"] == "confirmed"
        and all(products[pid][a] is False for a in required)
        for pid in linked)


def _shown_to_chef(deps):
    return {d["deal_id"] for p in deps.chef_llm.prompts
            for d in _json_after(p, "**Available Deals:**")}


RUNS = [(name, r) for name in COMMITTED for r in (["vegetarian"], ["vegan"], [])]


@pytest.mark.parametrize("name,restrictions", RUNS,
                         ids=[f"{n}-{'+'.join(r) or 'open'}" for n, r in RUNS])
def test_real_graph_runs_offline_on_snapshot(name, restrictions):
    snapshot = load_snapshot(name)
    deps = _deps(snapshot)
    state = _run(deps, restrictions)

    assert state["status"] == "completed", state["errors"]
    assert deps.deals.calls == [(POSTAL, restrictions)]
    assert len(state["approved_recipe_ids"]) >= 1

    # Gate: the Chef saw exactly the deals the snapshot's attributes clear.
    all_ids = {d["deal_id"] for d in snapshot.data["deals"]}
    shown = _shown_to_chef(deps)
    served = {d["deal_id"] for d in state["available_deals"]}
    if restrictions:
        expected = {i for i in all_ids if _clears(snapshot, i, restrictions)}
        assert served == expected
        assert all_ids - served, "snapshot should hold a deal this user is refused"
    else:
        assert served == all_ids
    assert shown <= served

    # Every approved recipe cites only served deals / offered staples, and the
    # verdict is the deterministic lookup on the snapshot's attributes.
    catalog = CitationCatalog.from_state(state)
    staple_ids = {s["product_id"] for s in snapshot.catalog().fetch_pantry_staples()}
    for rid in state["approved_recipe_ids"]:
        recipe = state["generated_recipes"][rid]
        for ing in recipe["ingredients"]:
            if ing.get("deal_id") is not None:
                assert ing["deal_id"] in served
            else:
                assert ing["product_id"] in staple_ids
        report = compliance_report(recipe, restrictions, catalog)
        assert report["approved"] is True
        assert state["validation_results"][rid]["approved"] is True
    if restrictions:
        cited = {int(k) for k in state["deal_products"]}
        assert cited == served & set(snapshot.linked_products())

    # Nothing written anywhere but the in-memory store.
    assert len(deps.recipe_store.saved) == len(state["approved_recipe_ids"])
    assert deps.tracker.calls[-1] == "finalize_run"


def test_flipping_a_snapshot_attribute_changes_what_the_graph_serves():
    """The snapshot's attributes decide, not the deal's name: mark one of
    seed27's vegetarian-served products as meat and its deal disappears from
    what the Chef is shown; nothing else changes."""
    base = load_snapshot("seed27")
    before = _deps(base)
    _run(before, ["vegetarian"])
    shown_before = {d["deal_id"] for d in before.deals.fetch_current_deals(POSTAL, ["vegetarian"])}

    victim = sorted(shown_before)[0]
    pid = next(l["product_id"] for l in base.data["deal_products"] if l["deal_id"] == victim)
    data = copy.deepcopy(base.data)
    for p in data["products"]:
        if p["product_id"] == pid:
            p["contains_meat"] = True
    flipped = Snapshot.from_dict(data)
    after = _deps(flipped)
    state = _run(after, ["vegetarian"])

    served_after = {d["deal_id"] for d in state["available_deals"]}
    affected = {l["deal_id"] for l in base.data["deal_products"] if l["product_id"] == pid}
    assert victim not in served_after
    assert served_after == shown_before - affected
    assert victim not in _shown_to_chef(after)
    assert state["status"] == "completed", state["errors"]
