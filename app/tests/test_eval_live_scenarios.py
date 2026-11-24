"""The live-data scenario set (``eval/scenarios/live-2026-11-23.json``) over
the first live snapshot (``l4g3e8-2026-11-23``). Offline: loads and checks
the set; recording it needs a live model (``scripts/run_eval.py record``)."""
from pathlib import Path

import pytest

from app.eval.runner import load_scenarios
from app.eval.snapshots import load_snapshot
from app.services import products, trays

SCENARIOS = Path(__file__).resolve().parents[2] / "eval" / "scenarios" / "live-2026-11-23.json"


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(SCENARIOS)


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot("l4g3e8-2026-11-23")


def _proposal(snapshot, restrictions):
    return trays.propose_trays("L4G3E8", list(restrictions),
                               fetch_deals=snapshot.deal_source().fetch_gated_deals,
                               fetch_labels=snapshot.fetch_labels,
                               fetch_pantry=snapshot.catalog().fetch_pantry_staples)


def test_every_scenario_runs_on_the_live_snapshot_with_supported_labels(scenarios):
    assert {s.snapshot for s in scenarios} == {"l4g3e8-2026-11-23"}
    assert len({s.name for s in scenarios}) == len(scenarios) >= 5
    for s in scenarios:
        assert s.user.postal_code == "L4G3E8"
        assert not products.unsupported_restrictions(s.user.dietary_restrictions), s.name


def test_every_scenario_has_an_anchor_to_plan_around(scenarios, snapshot):
    """A scenario with no anchor would be refused before any model call
    (SelectionError) and tell us nothing about generation."""
    for s in scenarios:
        assert _proposal(snapshot, s.user.dietary_restrictions).anchors, s.name


def test_restricted_trays_are_thin_because_few_deals_are_product_linked(snapshot):
    """Pinned finding (2026-11-23): only the deals linked to confirmed products
    can pass the dietary gate, so restricted users get thin trays -- product
    linking, not the pipeline, is the bottleneck. Kosher gets no anchor at
    all, which is why it is not in the scenario set."""
    linked = {l["deal_id"] for l in snapshot.data["deal_products"]}
    assert len(linked) < 60 < len(snapshot.data["deals"])
    assert len(_proposal(snapshot, []).anchors) == trays.N_ANCHORS
    assert len(_proposal(snapshot, ["vegetarian"]).anchors) <= 2
    assert _proposal(snapshot, ["kosher"]).anchors == []
