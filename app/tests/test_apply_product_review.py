"""The draft-apply script's pairing logic, and the committed 2026-11-24 draft.

No database: ``plan_decisions`` is pure, and the draft checks read committed
files only.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.services import product_proposer as pp
from app.services.products import ATTRIBUTES, normalize_name
from scripts import apply_product_review as apr

ROOT = Path(__file__).resolve().parents[2]
DRAFT = ROOT / "probe" / "2026-11-24"


def _proposal(raw, name, *, match=None, unmappable=False, pos=0):
    return pp.Proposal(
        normalized_name=normalize_name(raw), raw_example=raw,
        match_existing_product=match, canonical_name=name, category="x",
        attributes={a: True for a in ATTRIBUTES}, is_pantry_staple=False,
        confidence=0.9, reason="r", unmappable=unmappable, tile_position=pos)


def test_target_pairs_uses_match_name_and_splits_unmappable():
    confirm, reject = apr.target_pairs([
        _proposal("Fresh Eggs", "eggs", match="Eggs"),
        _proposal("Tile A or B", "a", pos=0),
        _proposal("Tile A or B", "b", pos=1),
        _proposal("garbled copy", "", unmappable=True),
    ])
    assert confirm == {("fresh eggs", "eggs"), ("tile a or b", "a"),
                       ("tile a or b", "b")}
    assert reject == {"garbled copy"}


def test_plan_decisions_touches_only_draft_pairs():
    confirm = {("tile a or b", "a"), ("tile a or b", "b"), ("eggs", "eggs")}
    rows = [
        {"alias_id": 1, "product_id": 10, "normalized_name": "tile a or b", "product_name": "a"},
        {"alias_id": 2, "product_id": 11, "normalized_name": "tile a or b", "product_name": "b"},
        # Someone else's proposal for the same string, another product: untouched.
        {"alias_id": 3, "product_id": 12, "normalized_name": "tile a or b", "product_name": "c"},
        # An unplaced alias for a name the draft did not reject: untouched.
        {"alias_id": 4, "product_id": None, "normalized_name": "tile a or b", "product_name": None},
        {"alias_id": 5, "product_id": None, "normalized_name": "garbled", "product_name": None},
    ]
    groups, rejects, unmatched = apr.plan_decisions(rows, confirm, {"garbled"})
    assert groups == {10: [1], 11: [2]}
    assert rejects == [5]
    assert unmatched == {("eggs", "eggs")}


def test_committed_draft_is_valid_and_fully_decided():
    data = json.loads((DRAFT / "labelled.json").read_text(encoding="utf-8"))
    names = {r["name"] for r in json.loads((DRAFT / "names.json").read_text(encoding="utf-8"))}
    assert data["labels"]
    for entry in data["labels"]:
        assert pp.validate_payload(entry) == [], entry["raw_name"]
        assert entry["raw_name"] in names
        for spec in entry.get("products") or [entry]:
            # No NULL attribute on anything confirmed from this draft.
            assert all(isinstance(spec[a], bool) for a in ATTRIBUTES)


def test_committed_draft_matches_its_generator():
    spec = importlib.util.spec_from_file_location("label_20261124", DRAFT / "label.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = json.loads((DRAFT / "names.json").read_text(encoding="utf-8"))
    labels, _ = mod.build(names)
    committed = json.loads((DRAFT / "labelled.json").read_text(encoding="utf-8"))["labels"]
    assert labels == committed
