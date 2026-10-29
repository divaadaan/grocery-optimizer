"""Scenario set v1 (ROADMAP Track E item 6): offline coverage assertions.

Guards ``eval/scenarios/v1.json`` so later edits cannot silently drop coverage,
and keeps the review table ``v1.md`` honest about offered-deal counts.
"""
import re
from pathlib import Path

import pytest

from app.eval.runner import load_scenarios
from app.eval.snapshots import load_snapshot, snapshot_path
from app.services.products import RESTRICTION_ATTRIBUTES

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_FILE = ROOT / "eval" / "scenarios" / "v1.json"
REVIEW_DOC = ROOT / "eval" / "scenarios" / "v1.md"
DEFAULT_NUM_MEALS = 7  # NewRecipeRequest default in app/models/schemas.py


@pytest.fixture(scope="module")
def scenarios():
    return load_scenarios(SCENARIO_FILE)


def _restr(s):
    return set(s.user.dietary_restrictions)


def test_loads_with_unique_names_and_defaults(scenarios):
    assert 15 <= len(scenarios) <= 24
    names = [s.name for s in scenarios]
    assert len(set(names)) == len(names)
    assert all(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", n) for n in names)
    # models are the owner's decision (E9): scenarios carry no llm override
    assert all(not s.llm for s in scenarios)


def test_labels_supported_and_snapshots_exist(scenarios):
    for s in scenarios:
        assert _restr(s) <= set(RESTRICTION_ATTRIBUTES), s.name
        assert snapshot_path(s.snapshot).exists(), s.name
        assert s.user.postal_code in load_snapshot(s.snapshot).postal_codes, s.name
        assert s.notes, s.name


def test_restriction_coverage(scenarios):
    seen = set().union(*(_restr(s) for s in scenarios))
    assert {"vegetarian", "vegan", "pescatarian", "gluten_free", "dairy_free",
            "nut_free", "kosher"} <= seen
    assert any(not s.user.dietary_restrictions for s in scenarios)
    combined = [s for s in scenarios if len(_restr(s)) >= 2]
    assert len(combined) >= 2
    assert any({"kosher", "nut_free"} <= _restr(s) for s in combined)
    assert any({"vegan", "gluten_free"} <= _restr(s) for s in combined)


def test_household_meals_and_snapshot_coverage(scenarios):
    assert {1, 2, 4} <= {s.user.household_size for s in scenarios}
    meals = {s.user.num_meals for s in scenarios}
    assert {1, 2, DEFAULT_NUM_MEALS} <= meals
    assert {"seed27", "week-2026-10-16", "sparse5"} <= {s.snapshot for s in scenarios}


def _budget_class(s):
    per = s.user.budget / (s.user.household_size * s.user.num_meals)
    return "tight" if per < 3 else "generous" if per > 10 else "mid" if per >= 4 else None


def test_budget_classes(scenarios):
    weekly = [s for s in scenarios if s.user.budget_period == "weekly"]
    for s in weekly:
        tags = {"tight", "mid", "generous"} & set(s.name.split("-"))
        assert len(tags) == 1, s.name
        assert _budget_class(s) in tags, s.name
    assert {t for s in weekly for t in s.name.split("-")} >= {"tight", "mid", "generous"}
    assert any(s.user.budget_period == "monthly" and s.user.budget_spent_this_period > 0
               for s in scenarios)


def test_sparse5_stresses_scarcity(scenarios):
    sparse = [s for s in scenarios if s.snapshot == "sparse5"]
    assert len(sparse) >= 3
    assert any(s.user.dietary_restrictions for s in sparse)
    for s in sparse:
        offered = _offered(s)
        assert offered <= 5


def test_kosher_is_exercised(scenarios):
    assert sum("kosher" in _restr(s) for s in scenarios) >= 2


def _offered(s):
    snap = load_snapshot(s.snapshot)
    return len(snap.deal_source().fetch_current_deals(
        s.user.postal_code, list(s.user.dietary_restrictions)))


def test_review_doc_matches_offered_counts(scenarios):
    text = REVIEW_DOC.read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        m = re.match(r"\| `([^`]+)` \|(.*)\|", line)
        if m:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows[m.group(1)] = cells
    assert set(rows) == {s.name for s in scenarios}
    for s in scenarios:
        total = len(load_snapshot(s.snapshot).data["deals"])
        assert rows[s.name][6] == f"{_offered(s)} / {total}", s.name
