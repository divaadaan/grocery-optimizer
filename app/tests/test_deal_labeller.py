"""``app/services/deal_labeller.py``: parsing, retry, fail-fast, persistence.

Deterministic: the model is a fake ``chat_fn`` or a recorded gpt-oss-120b
reply:

* ``fixtures/deal_labeller/gpt_oss_batch.json``, captured 2026-11-23 with
  PROMPT_VERSION v2 over a mixed batch that includes the prototype's
  mislabels: tea, coffee pods, Reese's.
* ``fixtures/deal_labeller/gpt_oss_batch_v4.json``, captured 2026-11-24 with
  PROMPT_VERSION v4 over the v2 mislabels: canned and dried legumes as
  pantry_staple (v2 listed "canned beans" under both protein and
  pantry_staple) and a pomelo inside a run of sugar rows labelled "sugar";
  the regressions a v3 relabel showed (rice and tortillas drifting to starch,
  a raw shrimp skewer to snack, flour to unknown); plus tofu, eggs (shelved
  under "Dairy & Eggs") and meat / produce / staple / drink controls.
  Rows the model labels inconsistently from run to run at v4 (a Reese's /
  Mini Eggs tile, a yogurt-dipped protein bar) are in the batch but not
  asserted; the PR body has the run-to-run notes.
"""
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app.agents.llm_chat import Reply
from app.config import settings
from app.services import deal_labeller as dl

FIXTURE = Path(__file__).parent / "fixtures" / "deal_labeller" / "gpt_oss_batch.json"
FIXTURE_V4 = FIXTURE.with_name("gpt_oss_batch_v4.json")


def _deal(i, name="Item", **kw):
    return {"deal_id": i, "product_name": f"{name} {i}", "sale_price": 1.99,
            "unit": None, "category": None, "store_name": "S", "chain": "C", **kw}


def _reply(labels, **kw):
    return Reply(text=json.dumps({"labels": labels}), input_tokens=10, output_tokens=5, **kw)


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._rows = []

    def execute(self, sql, params=None):
        self.db.executed.append((sql, params))
        self._rows = list(self.db.rows)

    def executemany(self, sql, rows):
        self.db.saved.extend(rows)

    def fetchall(self):
        return self._rows


class FakeDB:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []
        self.saved = []

    @contextmanager
    def get_cursor(self):
        yield FakeCursor(self)


# ------------------------------------------------------------ the prompt

def test_prompt_example_is_strict_json_without_comments():
    prompt = dl.build_prompt([_deal(1)])
    assert "//" not in prompt  # gpt-oss loops on commented JSON examples
    line = next(l for l in prompt.splitlines() if l.startswith('{"labels"'))
    example = json.loads(line)
    assert {e["role"] for e in example["labels"]} <= set(dl.ROLES)


def test_deal_row_is_compact():
    row = dl.deal_row({"deal_id": 7, "product_name": "  Chicken   Thighs ", "sale_price": 3.49,
                       "unit": "lb", "category": "Meat", "store_name": "X", "chain": "No Frills"})
    assert row == "7 | Chicken Thighs | $3.49/lb | Meat | No Frills"


# ------------------------------------------------------------ parsing

def test_parse_labels_validates_every_field():
    text = json.dumps({"labels": [
        {"id": 1, "item": "  Chicken  Thighs ", "role": "protein", "size_qty": None, "size_unit": None},
        {"id": 2, "item": "pasta", "role": "pantry_staple", "size_qty": 0.9, "size_unit": "kg"},
        {"id": 3, "item": "thing", "role": "gadget"},
        {"id": 4, "item": "", "role": "protein"},
        {"id": 99, "item": "stranger", "role": "protein"},
        {"id": "x", "item": "bad id", "role": "protein"},
        {"id": 5, "item": "juice", "role": "beverage", "size_qty": -1, "size_unit": "ml"},
        {"id": 6, "item": "eggs", "role": "protein", "size_qty": 12, "size_unit": "dozen"},
        "junk",
    ]})
    got = dl.parse_labels(text, [1, 2, 3, 4, 5, 6])
    assert got[1] == dl.Label("chicken thighs", "protein")
    assert got[2] == dl.Label("pasta", "pantry_staple", 900.0, "g")
    assert got[3].role == dl.UNKNOWN_ROLE
    assert 4 not in got and 99 not in got
    assert (got[5].size_qty, got[5].size_unit) == (None, None)
    assert (got[6].size_qty, got[6].size_unit) == (None, None)


@pytest.mark.parametrize("qty,unit,expected", [
    (1.36, "kg", (1360.0, "g")), (2, "L", (2000.0, "ml")), (12, "each", (12.0, "each")),
    (1, "lb", (453.59, "g")), (None, "g", (None, None)), ("abc", "g", (None, None)),
    (float("nan"), "g", (None, None)), (5, None, (None, None)),
])
def test_size_normalisation(qty, unit, expected):
    assert dl._norm_size(qty, unit) == expected


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"labels": "nope"}'])
def test_unparseable_replies_give_no_labels(text):
    assert dl.parse_labels(text, [1]) == {}


def test_recorded_gpt_oss_batch_parses_completely_and_fixes_prototype_mislabels():
    rec = json.loads(FIXTURE.read_text())
    deals = rec["deals"]
    got = dl.parse_labels(rec["reply_text"], [d["deal_id"] for d in deals])
    assert set(got) == {d["deal_id"] for d in deals}
    roles = {d["deal_id"]: got[d["deal_id"]].role for d in deals}
    by_id = {d["deal_id"]: d for d in deals}
    # The prototype (prompt v1) labelled tea, coffee pods and Reese's as
    # pantry_staple. These are ids in the recorded batch, asserted by id.
    assert roles[1360] == "beverage"   # coffee pods
    assert roles[1390] == "beverage"   # herbal tea
    assert roles[1263] == "snack"      # Reese's
    assert roles[740] == "protein"     # chicken thighs
    assert got[1199] == dl.Label("olive oil", "pantry_staple", 750.0, "ml")
    assert got[1208].size_qty == 9070.0 and got[1208].size_unit == "g"
    assert "pantry_staple" not in {roles[i] for i in (1302, 1303, 1304, 1305, 1316, 1339)}
    assert all(by_id[i] for i in roles)


def test_recorded_v4_batch_labels_legumes_as_protein_and_fruit_as_fruit():
    rec = json.loads(FIXTURE_V4.read_text())
    assert rec["prompt_version"] == dl.PROMPT_VERSION
    deals = rec["deals"]
    got = dl.parse_labels(rec["reply_text"], [d["deal_id"] for d in deals])
    assert set(got) == {d["deal_id"] for d in deals}
    roles = {i: lab.role for i, lab in got.items()}
    # v2 mislabels, asserted by id in the recorded batch: canned beans, mixed
    # bean tiles and dried chickpeas were pantry_staple; the pomelo was sugar.
    legumes = (647, 648, 1133, 1174, 1175, 1176)
    assert {roles[i] for i in legumes} == {"protein"}
    assert got[1224].role == "fruit" and got[1224].item != "sugar"
    # Plant proteins and eggs stay protein; eggs on a "Dairy & Eggs" shelf are not dairy.
    assert {roles[i] for i in (1266, 1267, 1268, 1269, 1272, 902, 906, 907, 910)} == {"protein"}
    # v3-relabel regressions: a raw shrimp skewer is protein; rice and flour
    # are staples; potatoes are starch (v4 defines starch).
    assert roles[822] == "protein"
    assert {roles[i] for i in (1084, 1088, 1208, 1211, 1212)} == {"pantry_staple"}
    assert {roles[i] for i in (585, 598)} == {"starch"}
    # Controls: no regression on meat, produce, staples or drinks.
    assert roles[740] == "protein"
    assert roles[568] == roles[573] == "vegetable"
    assert {roles[i] for i in (1222, 1223, 1225, 1226, 1227, 1228, 1229)} == {"pantry_staple"}
    assert roles[1199] in {"pantry_staple", "condiment"}  # olive oil: either is STOCK-UP
    assert {roles[i] for i in (1360, 1390)} == {"beverage"}


def test_prompt_lists_legumes_only_under_protein():
    lines = dl.PROMPT.splitlines()
    staple = next(i for i, l in enumerate(lines) if l.strip().startswith("pantry_staple ="))
    # the staple rule names legumes only to exclude them (v2 listed canned beans as a staple)
    assert "canned beans" not in " ".join(lines[staple:staple + 3])


# ------------------------------------------------------------ batching / retry

def test_label_batch_retries_only_the_missing_ids_once():
    calls = []

    def chat_fn(spec, messages, **kw):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            return _reply([{"id": 1, "item": "a", "role": "fruit"}])
        return _reply([{"id": 2, "item": "b", "role": "fruit"}])

    report = dl.LabelReport()
    got = dl.label_batch([_deal(1), _deal(2), _deal(3)], spec="hf:x/y", chat_fn=chat_fn,
                         report=report)
    assert set(got) == {1, 2}
    assert len(calls) == 2 and report.retries == 1
    assert "1 | Item 1" not in calls[1] and "2 | Item 2" in calls[1]
    assert report.input_tokens == 20


def test_label_batch_uses_deterministic_json_settings():
    seen = {}

    def chat_fn(spec, messages, **kw):
        seen.update(kw, spec=spec)
        return _reply([{"id": 1, "item": "a", "role": "fruit"}])

    dl.label_batch([_deal(1)], spec="hf:x/y", chat_fn=chat_fn)
    assert seen == {"spec": "hf:x/y", "temperature": 0.0, "max_tokens": dl.MAX_TOKENS,
                    "json_mode": True}


# ------------------------------------------------------------ the run

def test_label_pending_saves_each_batch_and_reports():
    db = FakeDB(rows=[_deal(i) for i in range(1, 6)])

    def chat_fn(spec, messages, **kw):
        ids = [int(l.split(" | ")[0]) for l in messages[0]["content"].splitlines()
               if l[:1].isdigit()]
        return _reply([{"id": i, "item": f"item {i}", "role": "vegetable"} for i in ids])

    report = dl.label_pending("L4G3E8", spec="hf:x/y", chat_fn=chat_fn, database=db,
                              batch_size=2)
    assert (report.pending, report.labelled, report.unlabelled, report.batches) == (5, 5, 0, 3)
    assert report.by_role == {"vegetable": 5}
    assert sorted(r["deal_id"] for r in db.saved) == [1, 2, 3, 4, 5]
    assert {r["version"] for r in db.saved} == {dl.PROMPT_VERSION}
    assert {r["model"] for r in db.saved} == {"hf:x/y"}
    sql, params = db.executed[0]
    assert params == {"version": dl.PROMPT_VERSION, "postal_code": "L4G3E8"}
    assert "valid_until >= CURRENT_DATE" in sql


def test_label_pending_stops_on_infrastructure_failure():
    db = FakeDB(rows=[_deal(i) for i in range(1, 7)])
    calls = []

    def chat_fn(spec, messages, **kw):
        calls.append(1)
        request = httpx.Request("POST", "https://router.huggingface.co")
        raise httpx.HTTPStatusError("402", request=request,
                                    response=httpx.Response(402, request=request))

    report = dl.label_pending(spec="hf:x/y", chat_fn=chat_fn, database=db, batch_size=2)
    assert len(calls) == 1
    assert report.stopped_on_infrastructure.startswith("HTTPStatusError")
    assert (report.labelled, report.unlabelled) == (0, 6)
    assert db.saved == []


def test_label_pending_skips_a_batch_that_fails_as_model_output():
    db = FakeDB(rows=[_deal(i) for i in range(1, 5)])
    calls = []

    def chat_fn(spec, messages, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("model said something odd")
        ids = [int(l.split(" | ")[0]) for l in messages[0]["content"].splitlines()
               if l[:1].isdigit()]
        return _reply([{"id": i, "item": "x", "role": "fruit"} for i in ids])

    report = dl.label_pending(spec="hf:x/y", chat_fn=chat_fn, database=db, batch_size=2)
    assert report.stopped_on_infrastructure is None
    assert (report.labelled, report.unlabelled) == (2, 2)


def test_label_pending_rejects_a_bad_spec_before_reading_the_database():
    db = FakeDB()
    with pytest.raises(ValueError):
        dl.label_pending(spec="gpt-4o", database=db)
    assert db.executed == []


def test_save_labels_is_an_upsert_at_the_current_version():
    db = FakeDB()
    n = dl.save_labels({3: dl.Label("rice", "pantry_staple", 900.0, "g")}, model="m", database=db)
    assert n == 1
    assert db.saved == [{"deal_id": 3, "item": "rice", "role": "pantry_staple",
                         "size_qty": 900.0, "size_unit": "g", "model": "m",
                         "version": dl.PROMPT_VERSION}]
    assert "ON CONFLICT (deal_id) DO UPDATE" in dl._UPSERT_SQL


# ------------------------------------------------------------ background hook

def test_background_labelling_is_off_when_disabled():
    assert settings.deal_labelling_enabled is False  # conftest guard
    assert dl.start_background_labelling("X", runner=lambda pc: pytest.fail("ran")) is None


def test_background_labelling_runs_once_and_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "deal_labelling_enabled", True)
    gate = threading.Event()
    ran = []

    def runner(postal_code):
        ran.append(postal_code)
        gate.wait(5)
        raise RuntimeError("boom")  # must be swallowed

    t = dl.start_background_labelling("L4G3E8", runner=runner)
    assert t is not None
    # a second run while the first is going is skipped, not queued
    assert dl.start_background_labelling("L4G3E8", runner=runner) is None
    gate.set()
    t.join(5)
    assert ran == ["L4G3E8"]
    # the lock is released after a failure
    t2 = dl.start_background_labelling("L4G3E8", runner=lambda pc: dl.LabelReport())
    t2.join(5)

