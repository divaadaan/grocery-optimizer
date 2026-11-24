"""``app/services/trays.py`` and ``POST /plans/trays``: deterministic trays
from gated deals and stored labels. No database, no model."""
import asyncio

import httpx
import pytest
from fastapi import FastAPI

from app.routes import plans as plans_route
from app.services import trays
from app.services.deal_labeller import Label


def deal(i, price, chain, unit=None, name=None):
    return {"deal_id": i, "product_name": name or f"deal {i}", "sale_price": price,
            "unit": unit, "category": None, "store_name": f"{chain} #1", "chain": chain}


L = Label


# ------------------------------------------------------------ price basis

@pytest.mark.parametrize("unit,label,expected", [
    ("lb", None, ("lb", 3.0)),
    ("LB.", None, ("lb", 3.0)),
    ("100 g", None, ("lb", round(3.0 * 4.53592, 4))),
    ("6.61/kg", None, ("lb", 3.0)),
    (None, L("rice", "pantry_staple", 900.0, "g"), ("lb", round(3.0 / 900 * 453.592, 4))),
    (None, L("juice", "beverage", 1750.0, "ml"), ("l", round(3.0 / 1750 * 1000, 4))),
    (None, L("eggs", "protein", 12.0, "each"), ("each", 0.25)),
    (None, L("bread", "starch"), ("pkg", 3.0)),
    ("each", None, ("pkg", 3.0)),
])
def test_price_basis(unit, label, expected):
    basis, price = trays.price_basis(deal(1, 3.0, "A", unit=unit), label)
    assert (basis, round(price, 4)) == expected


def test_price_basis_without_a_price_is_none():
    assert trays.price_basis({"deal_id": 1, "sale_price": None}, None) is None


# ------------------------------------------------------------ grouping

def test_cheapest_per_item_with_cross_chain_gap():
    deals = [deal(1, 2.0, "No Frills", "lb"), deal(2, 4.0, "Metro", "lb"),
             deal(3, 5.0, "Sobeys", "lb"), deal(4, 4.5, "Metro", "lb")]
    labels = {i: L("chicken thighs", "protein") for i in (1, 2, 3, 4)}
    p = trays.build_trays(deals, labels)
    assert [t.deal_id for t in p.anchors] == [1]
    a = p.anchors[0]
    # per-chain cheapest: 2.0, 4.0, 5.0 -> median 4.0 -> gap 0.5
    assert (a.n_chains, a.median_price, a.gap, a.basis) == (3, 4.0, 0.5, "lb")


def test_same_item_on_different_bases_is_not_compared_and_shown_once():
    deals = [deal(1, 2.0, "A", "lb"), deal(2, 9.0, "B"), deal(3, 12.0, "C")]
    labels = {i: L("chicken thighs", "protein") for i in (1, 2, 3)}
    best = trays._group_best(deals, labels)
    # lb and pkg are separate groups: deal 1 is never compared with 2 and 3
    assert {(t.deal_id, t.basis, t.n_chains) for t in best} == {(1, "lb", 1), (2, "pkg", 2)}
    # ...and the tray shows the item once: the lb row, since a pkg group has
    # no gap and ranks after every per-unit basis
    p = trays.build_trays(deals, labels)
    assert [(t.deal_id, t.basis) for t in p.anchors] == [(1, "lb")]


def test_pkg_groups_make_no_gap_claim_but_keep_chain_count_and_median():
    # The live case: a $1.99 skewer against $6.67-$19.99 bags, all per package.
    deals = [deal(1, 1.99, "A"), deal(2, 6.67, "B"), deal(3, 18.99, "C"), deal(4, 19.99, "D")]
    labels = {i: L("shrimp", "protein") for i in (1, 2, 3, 4)}
    (row,) = trays._group_best(deals, labels)
    assert (row.deal_id, row.basis, row.gap) == (1, "pkg", 0.0)
    assert (row.n_chains, row.median_price) == (4, 12.83)  # data only, never a claim


def test_pkg_rows_rank_after_per_unit_rows_then_by_price():
    deals = [
        # pkg groups with big would-be gaps
        deal(1, 1.0, "A", name="skewer"), deal(2, 20.0, "B"),
        deal(3, 2.0, "A"), deal(4, 30.0, "B"),
        # per-unit groups: one with a gap, one single-chain (gap 0)
        deal(5, 3.0, "A", "lb"), deal(6, 4.0, "B", "lb"),
        deal(7, 3.6, "C"),
    ]
    labels = {1: L("shrimp", "protein"), 2: L("shrimp", "protein"),
              3: L("tofu", "protein"), 4: L("tofu", "protein"),
              5: L("pork loin", "protein"), 6: L("pork loin", "protein"),
              7: L("eggs", "protein", 12.0, "each")}
    p = trays.build_trays(deals, labels)
    assert [(t.deal_id, t.basis) for t in p.anchors] == [
        (5, "lb"),    # gap 0.14
        (7, "each"),  # gap 0 (one chain), but a per-unit basis
        (1, "pkg"),   # pkg rows last, cheapest first
        (3, "pkg"),
    ]
    assert [t.gap for t in p.anchors if t.basis == "pkg"] == [0.0, 0.0]


def test_roles_decide_the_tray_and_excluded_roles_never_appear():
    roles = ["protein", "vegetable", "starch", "fruit", "dairy", "pantry_staple", "condiment",
             "prepared_meal", "snack", "beverage", "non_food", "unknown"]
    deals, labels = [], {}
    for i, role in enumerate(roles, start=1):
        for j, chain in enumerate(("A", "B")):
            did = i * 10 + j
            deals.append(deal(did, 1.0 + j, chain))
            labels[did] = L(f"item {i}", role)
    p = trays.build_trays(deals, labels)
    assert {t.role for t in p.anchors} == {"protein"}
    assert {t.role for t in p.sides} == {"vegetable", "starch", "fruit", "dairy"}
    assert {t.role for t in p.stock_up} == {"pantry_staple", "condiment"}
    offered_roles = {p.by_id()[i].role for i in p.offered_ids()}
    assert not offered_roles & {"prepared_meal", "snack", "beverage", "non_food", "unknown"}


def test_stock_up_needs_two_chains_but_anchors_do_not():
    deals = [deal(1, 2.0, "A"), deal(2, 3.0, "A")]
    labels = {1: L("pasta", "pantry_staple"), 2: L("tofu", "protein")}
    p = trays.build_trays(deals, labels)
    assert p.stock_up == [] and [t.deal_id for t in p.anchors] == [2]


def test_unlabelled_deals_never_reach_a_tray_and_are_counted():
    deals = [deal(1, 2.0, "A", "lb"), deal(2, 2.0, "B", "lb")]
    p = trays.build_trays(deals, {1: L("pork chops", "protein")})
    assert p.offered_ids() == [1]
    assert p.counts["gated_deals"] == 2 and p.counts["unlabelled"] == 1
    assert p.counts["in_trays"] == 1


def test_order_is_gap_then_basis_then_price_and_tray_sizes_cap():
    deals, labels = [], {}
    for i in range(40):
        deals += [deal(1000 + i, 1.0, "A", "lb"), deal(2000 + i, 1.0 + i / 10, "B", "lb")]
        labels[1000 + i] = labels[2000 + i] = L(f"meat {i}", "protein")
    p = trays.build_trays(deals, labels)
    assert len(p.anchors) == trays.N_ANCHORS
    # ordered by gap rounded to whole percents (ties: basis, price, deal_id)
    gaps = [round(t.gap, 2) for t in p.anchors]
    assert gaps == sorted(gaps, reverse=True)
    assert {t.item for t in p.anchors} == {f"meat {i}" for i in range(25, 40)}


def test_build_is_deterministic_under_input_order():
    deals = [deal(i, 1.0 + (i % 7) / 3, "ABC"[i % 3], "lb") for i in range(1, 60)]
    labels = {i: L(f"item {i % 11}", ["protein", "vegetable", "pantry_staple"][i % 3])
              for i in range(1, 60)}
    a = trays.build_trays(deals, labels).as_dict()
    b = trays.build_trays(list(reversed(deals)), labels).as_dict()
    assert a == b


def test_tray_row_for_the_planner():
    t = trays.TrayItem(deal_id=7, product_name="CHICKEN  THIGHS", store_name="NF", chain="No Frills",
                       sale_price=1.99, unit="lb", item="chicken thighs", role="protein",
                       basis="lb", compare_price=1.99, median_price=3.49, n_chains=3, gap=0.43)
    assert trays.tray_row(t) == ("7 | CHICKEN THIGHS | $1.99/lb | No Frills | item: chicken "
                                 "thighs; cheapest of 3 chains (median $3.49/lb)")


def test_tray_row_for_a_pkg_row_claims_no_comparison():
    t = trays.TrayItem(deal_id=8, product_name="White Shrimp Skewer", store_name="S", chain="C",
                       sale_price=1.99, unit=None, item="shrimp", role="protein", basis="pkg",
                       compare_price=1.99, median_price=6.67, n_chains=5, gap=0.0)
    row = trays.tray_row(t)
    assert row.endswith("item: shrimp; sold by 5 chains; package sizes differ, not compared")
    assert "cheapest" not in row and "median" not in row
    single = trays.tray_row(trays.TrayItem(**{**t.__dict__, "n_chains": 1}))
    assert single.endswith("item: shrimp; only chain with it")


# ------------------------------------------------------------ pantry + proposal

def _staple(pid, name, **attrs):
    base = {"product_id": pid, "canonical_name": name, "status": "confirmed",
            "contains_meat": False, "contains_fish": False, "contains_dairy": False,
            "contains_egg": False, "contains_nuts": False, "contains_animal_byproduct": False,
            "not_kosher": False}
    base.update(attrs)
    return base


def test_pantry_offers_only_confirmed_staples_that_clear_the_restrictions():
    staples = [_staple(1, "olive oil"), _staple(2, "butter", contains_dairy=True,
                                                contains_animal_byproduct=True),
               _staple(3, "fish sauce", contains_fish=True),
               _staple(4, "salt", status="proposed"),
               _staple(5, "honey", contains_animal_byproduct=None)]
    assert [p["product_id"] for p in trays.offered_pantry(staples, [])] == [1, 2, 3, 5]
    assert trays.offered_pantry(staples, ["vegan"]) == [{"product_id": 1, "name": "olive oil"}]


def test_propose_trays_passes_the_restrictions_to_the_gate():
    calls = {}

    def fetch_deals(postal, restrictions):
        calls["deals"] = (postal, restrictions)
        return [deal(1, 2.0, "A", "lb")]

    def fetch_labels(ids):
        calls["labels"] = list(ids)
        return {1: L("tofu", "protein")}

    p = trays.propose_trays("L4G3E8", ["vegan"], fetch_deals=fetch_deals,
                            fetch_labels=fetch_labels,
                            fetch_pantry=lambda: [_staple(9, "rice")])
    assert calls == {"deals": ("L4G3E8", ["vegan"]), "labels": [1]}
    assert p.offered_ids() == [1] and p.pantry == [{"product_id": 9, "name": "rice"}]


# ------------------------------------------------------------ the route

def _post(path, json):
    app = FastAPI()
    app.include_router(plans_route.router)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver") as c:
            return await c.post(path, json=json)

    return asyncio.run(go())


@pytest.fixture
def user(monkeypatch):
    profile = {"user_id": 1, "postal_code": "L4G3E8", "dietary_restrictions": []}
    monkeypatch.setattr(plans_route.UserService, "get_user_by_id",
                        staticmethod(lambda uid: dict(profile) if uid == 1 else None))
    return profile


def test_route_returns_trays_pantry_and_warnings(monkeypatch, user):
    proposal = trays.build_trays(
        [deal(1, 2.0, "A", "lb"), deal(2, 1.0, "A")],
        {1: L("chicken thighs", "protein"), 2: L("broccoli", "vegetable")})
    proposal.pantry = [{"product_id": 5, "name": "salt"}]
    monkeypatch.setattr(plans_route, "propose_for_user", lambda u: proposal)
    r = _post("/plans/trays", {"user_id": 1, "num_meals": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [t["deal_id"] for t in body["trays"]["anchors"]] == [1]
    assert [t["deal_id"] for t in body["trays"]["sides"]] == [2]
    assert body["pantry"] == [{"product_id": 5, "name": "salt"}]
    assert body["counts"]["in_trays"] == 2
    assert any("more than one dish" in w for w in body["warnings"])


def test_route_404_for_an_unknown_user(user):
    assert _post("/plans/trays", {"user_id": 2}).status_code == 404


def test_route_422_for_an_unsupported_restriction(monkeypatch, user):
    user["dietary_restrictions"] = ["halal"]
    monkeypatch.setattr(plans_route, "propose_for_user",
                        lambda u: pytest.fail("must refuse before building trays"))
    r = _post("/plans/trays", {"user_id": 1})
    assert r.status_code == 422 and "halal" in r.text


def test_route_warns_when_there_is_no_anchor(monkeypatch, user):
    monkeypatch.setattr(plans_route, "propose_for_user",
                        lambda u: trays.build_trays([], {}))
    body = _post("/plans/trays", {"user_id": 1}).json()
    assert any("No protein deals" in w for w in body["warnings"])
    assert any("No current deals" in w for w in body["warnings"])


# ------------------------------------------------------------ sides quota

def _sides_market(n_per_role):
    deals, labels = [], {}
    did = 1
    for role, n in n_per_role.items():
        for i in range(n):
            # dairy gets the biggest gaps so it would win every slot unquota'd
            gap_price = 1.0 if role == "dairy" else 2.0
            for chain, price in (("A", gap_price), ("B", 4.0)):
                deals.append(deal(did, price, chain, "lb"))
                labels[did] = L(f"{role} {i}", role)
                did += 1
    return deals, labels


def test_side_quotas_sum_to_the_tray_size():
    assert sum(trays.SIDE_QUOTAS.values()) == trays.N_SIDES
    assert set(trays.SIDE_QUOTAS) == trays.SIDE_ROLES


def test_dairy_cannot_crowd_out_the_vegetables():
    deals, labels = _sides_market({"dairy": 30, "vegetable": 30, "starch": 30, "fruit": 30})
    sides = trays.build_trays(deals, labels).sides
    by_role = {}
    for t in sides:
        by_role[t.role] = by_role.get(t.role, 0) + 1
    assert by_role == trays.SIDE_QUOTAS
    gaps = [round(t.gap, 2) for t in sides]
    assert gaps == sorted(gaps, reverse=True)  # still in tray order


def test_unused_quota_goes_to_the_best_leftovers():
    deals, labels = _sides_market({"dairy": 30, "vegetable": 3, "starch": 0, "fruit": 0})
    sides = trays.build_trays(deals, labels).sides
    assert len(sides) == trays.N_SIDES
    assert sum(t.role == "vegetable" for t in sides) == 3
    assert sum(t.role == "dairy" for t in sides) == 22


def test_an_item_labelled_with_two_roles_appears_once_per_tray():
    deals = [deal(1, 1.0, "A", "lb"), deal(2, 2.0, "B", "lb")]
    labels = {1: L("tomato", "vegetable"), 2: L("tomato", "fruit")}
    assert [t.deal_id for t in trays.build_trays(deals, labels).sides] == [1]
