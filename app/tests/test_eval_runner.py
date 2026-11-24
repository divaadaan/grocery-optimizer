"""Evaluation runner with record/replay (ROADMAP Track E §3), on the
approach-C pipeline.

Deterministic and offline: "recording" uses scripted chat functions (their
replies are computed from the request), and replay serves from call logs.
Any database or MLflow access fails the test.

Regenerate the committed replay fixture (only when the pipeline's prompts or
result legitimately change -- the diff is the review)::

    ~/projects/grocery-optimizer/.venv/bin/python -m app.tests.test_eval_runner
"""
import copy
import json
import random
import re
import threading
import time
from pathlib import Path
from typing import List

import pytest

from app.agents.llm_chat import Reply
from app.eval import recorder, runner
from app.eval.recorder import (
    CallLog,
    ContentRecipeIds,
    RecordingChat,
    ReplayChat,
    ReplayLeftover,
    ReplayLog,
    ReplayMismatch,
    ReplayedLLMError,
    canonical_messages,
    request_sha256,
)
from app.eval.runner import (
    ArtifactError,
    Scenario,
    ScenarioError,
    SnapshotDrift,
    compare_artifacts,
    diff_values,
    dump_artifact,
    load_artifact,
    load_scenarios,
    validate_artifact,
)
from scripts import run_eval

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eval_runs"
SCENARIOS = FIXTURES / "scenarios.json"
COMMITTED_ARTIFACT = FIXTURES / "seed27-vegetarian.json"
SNAPSHOTS = ("seed27", "week-2026-10-16")
POSTAL = "M5V3A8"


@pytest.fixture(autouse=True)
def no_database_no_mlflow(monkeypatch):
    from app.db.database import Database
    from app.services import mlflow_logger

    def no_db(*args, **kwargs):
        raise AssertionError("eval test touched the database")

    def no_mlflow():
        raise AssertionError("eval test touched MLflow")

    monkeypatch.setattr(Database, "initialize", no_db)
    monkeypatch.setattr(Database, "get_connection", no_db)
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", no_mlflow)


# --------------------------------------------------------------------------
# Scripted "live" models: chat functions answering from the request
# --------------------------------------------------------------------------

_TRAY_ROW = re.compile(r"^(\d+) \| ", re.M)
_CITE_DEAL = re.compile(r"→ deal_id (\d+) \(")
_CITE_STAPLE = re.compile(r"→ product_id (\d+)$", re.M)


def _section(text: str, header: str, end: str) -> str:
    start = text.index(header)
    stop = text.find(end, start + len(header))
    return text[start:stop if stop >= 0 else None]


def _usage(messages, content):
    n_in = sum(len(m["content"]) for m in messages) // 4
    return n_in, len(content) // 4


def chef_reply(texts: List[str]) -> str:
    """One dish per anchor (cycling), each with the next side."""
    prompt = texts[0]
    anchors = [int(x) for x in _TRAY_ROW.findall(_section(prompt, "ANCHORS (", "SIDES ("))]
    sides = [int(x) for x in _TRAY_ROW.findall(_section(prompt, "SIDES (", "STOCK-UP ("))]
    n = int(re.search(r"exactly (\d+) dishes", prompt).group(1))
    dishes = [{"name": f"Dish {i + 1}", "anchor_deal_ids": [anchors[i % len(anchors)]],
               "side_deal_ids": [sides[i % len(sides)]] if sides else [],
               "pantry_product_ids": [], "why": "scripted"} for i in range(n)]
    return json.dumps({"dishes": dishes, "stock_up_deal_ids": [], "notes": "scripted"})


def sous_chef_reply(texts: List[str]) -> str:
    """A recipe citing every deal of the dish plus the first staple. A dish
    whose first deal id is even answers its *first* attempt with non-JSON,
    so recordings hold validate-and-retry conversations too."""
    prompt = texts[0]
    dish = _section(prompt, "**This dish's ingredients", "**Stock-up")
    deals = [int(x) for x in _CITE_DEAL.findall(dish)]
    staples = [int(x) for x in _CITE_STAPLE.findall(_section(prompt, "**Pantry staples",
                                                             "**Requirements"))]
    if len(texts) == 1 and deals[0] % 2 == 0:
        return "Sure! Here is a recipe: (not JSON)"
    ings = [{"name": "x", "deal_id": d, "quantity": "1", "unit": "lb"} for d in deals]
    ings += [{"name": "y", "product_id": p, "quantity": "1", "unit": "tsp"} for p in staples[:1]]
    name = re.search(r"\*\*The dish:\*\* (.+)", prompt).group(1)
    return json.dumps({"name": f"{name} ({'+'.join(map(str, deals))})", "meal_type": "dinner",
                       "servings": 2, "estimated_prep_time": "20 minutes", "ingredients": ings,
                       "instructions": ["Combine " + ", ".join(
                           f"{{{i}}}" for i in range(len(ings))) + ".", "Cook it."]})


def scripted_chat(respond):
    def chat(spec, messages, **params):
        content = respond([m["content"] for m in messages])
        n_in, n_out = _usage(messages, content)
        return Reply(text=content, input_tokens=n_in, output_tokens=n_out,
                     finish_reason="stop", model=spec)
    chat.__qualname__ = f"scripted_chat.{respond.__name__}"
    return chat


def scripted_factory(role, spec):
    return scripted_chat({"chef": chef_reply, "sous_chef": sous_chef_reply}[role])


def scenario(snapshot="seed27", restrictions=("vegetarian",), **user) -> Scenario:
    base = {"postal_code": POSTAL, "household_size": 2, "num_meals": 3,
            "dietary_restrictions": list(restrictions)}
    base.update(user)
    return Scenario.from_dict({
        "name": f"{snapshot}-{'+'.join(restrictions) or 'open'}", "snapshot": snapshot,
        "user": base,
        "llm": {"chef": {"provider": "scripted", "model": "chef-script"},
                "sous_chef": {"provider": "scripted", "model": "sous-script"}}})


def record(sc: Scenario):
    return runner.record(sc, chat_factory=scripted_factory)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def test_scenario_round_trips_and_fills_defaults():
    sc = Scenario.from_dict({"name": "x", "snapshot": "seed27",
                             "user": {"postal_code": POSTAL, "household_size": 1,
                                      "num_meals": 2}})
    d = sc.to_dict()
    assert d["user"] == {"user_id": 1, "postal_code": POSTAL, "household_size": 1,
                         "num_meals": 2, "dietary_restrictions": [], "preferences": {},
                         "kept_deal_ids": None, "pantry_product_ids": None}
    assert d["llm"] == {} and d["notes"] is None
    assert Scenario.from_dict(d) == sc
    assert Scenario.from_dict(json.loads(json.dumps(d))).to_dict() == d


def test_scenario_carries_a_confirmed_selection():
    sc = scenario(kept_deal_ids=[3, 1], pantry_product_ids=[9])
    kw = sc.user.run_kwargs()
    assert kw["kept_deal_ids"] == [3, 1] and kw["pantry_product_ids"] == [9]
    assert Scenario.from_dict(sc.to_dict()) == sc


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d.update(colour="red"), "unexpected field"),
    (lambda d: d.pop("snapshot"), "missing field"),
    (lambda d: d["user"].pop("household_size"), "missing field"),
    (lambda d: d["user"].update(budget=80.0), "unexpected field"),   # budget-agnostic
    (lambda d: d["user"].update(user_name="x"), "unexpected field"),
    (lambda d: d["user"].update(household_size="2"), "household_size"),
    (lambda d: d["user"].update(num_meals=True), "num_meals"),
    (lambda d: d["user"].update(num_meals=0), "num_meals"),
    (lambda d: d["user"].update(kept_deal_ids=["1"]), "kept_deal_ids"),
    (lambda d: d["user"].update(dietary_restrictions=["vegan", 3]), "dietary_restrictions"),
    (lambda d: d["llm"].update(nutritionist={"provider": "a", "model": "b"}), "unknown role"),
    (lambda d: d["llm"].update(chef={"provider": "ollama"}), "missing field"),
])
def test_malformed_scenarios_are_refused(mutate, match):
    d = scenario().to_dict()
    mutate(d)
    with pytest.raises(ScenarioError, match=match):
        Scenario.from_dict(d)


def test_scenario_files_json_yaml_and_lists(tmp_path):
    one = scenario().to_dict()
    two = dict(copy.deepcopy(one), name="other")
    (tmp_path / "a.json").write_text(json.dumps(one))
    (tmp_path / "b.json").write_text(json.dumps({"scenarios": [one, two]}))
    yaml = pytest.importorskip("yaml")
    (tmp_path / "c.yaml").write_text(yaml.safe_dump([one, two]))
    assert [s.name for s in load_scenarios(tmp_path / "a.json")] == [one["name"]]
    assert [s.name for s in load_scenarios(tmp_path / "b.json")] == [one["name"], "other"]
    assert load_scenarios(tmp_path / "c.yaml") == load_scenarios(tmp_path / "b.json")
    (tmp_path / "d.json").write_text(json.dumps([one, one]))
    with pytest.raises(ScenarioError, match="duplicate"):
        load_scenarios(tmp_path / "d.json")


def test_committed_scenario_file_describes_the_committed_artifact():
    (sc,) = load_scenarios(SCENARIOS)
    assert load_artifact(COMMITTED_ARTIFACT)["scenario"] == sc.to_dict()


def test_resolved_spec_defaults_to_the_settings():
    from app.config import settings
    assert runner.resolved_spec("chef", None) == settings.llm_planner_model
    assert runner.resolved_spec("sous_chef", None) == settings.llm_writer_model
    assert runner.resolved_spec("chef", runner.RoleModel("hf", "a/b")) == "hf:a/b"


# --------------------------------------------------------------------------
# Recorder / replayer units
# --------------------------------------------------------------------------

def _user(text):
    return [{"role": "user", "content": text}]


def test_canonical_request_hash_is_content_only():
    a = canonical_messages([{"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "x"}])
    assert a == [{"type": "user", "content": "hi"}, {"type": "assistant", "content": "x"}]
    assert request_sha256(a) == request_sha256(json.loads(json.dumps(a)))
    assert request_sha256(a) != request_sha256(a[:1])


def test_recorder_logs_usage_only_when_reported_and_records_errors():
    log = CallLog()
    with_usage = RecordingChat(scripted_chat(lambda t: "{}"), "chef", log, "p:m")
    without = RecordingChat(lambda spec, m, **k: Reply(text="{}"), "sous_chef", log, "p:n")
    with_usage("ignored", _user("abcdefgh"), temperature=0.1)
    without("ignored", _user("abcdefgh"))

    def boom(spec, messages, **params):
        raise ConnectionError("model server down")

    failing = RecordingChat(boom, "sous_chef", log, "p:n")
    with pytest.raises(ConnectionError):
        failing("ignored", _user("q"))

    first, second, third = log.calls
    assert first["response"]["usage"] == {"input_tokens": 2, "output_tokens": 0,
                                          "total_tokens": 2}
    assert first["model"]["provider"] == "p" and first["model"]["model"] == "m"
    assert first["model"]["params"] == {"temperature": 0.1}
    assert first["response"]["response_metadata"]["finish_reason"] == "stop"
    assert second["response"]["usage"] is None  # not reported -> null, not estimated
    assert third["response"] is None
    assert third["error"] == {"type": "ConnectionError", "message": "model server down"}
    assert [c["latency_s"] >= 0 for c in log.calls] == [True] * 3


def test_recorder_calls_the_role_spec_not_the_callers():
    seen = []
    rec = RecordingChat(lambda spec, m, **k: seen.append(spec) or Reply(text="{}"),
                        "chef", CallLog(), "hf:org/model")
    rec("ollama:whatever", _user("q"))
    assert seen == ["hf:org/model"]


def test_recorder_is_thread_safe_and_counts_occurrences():
    log = CallLog()
    chat = RecordingChat(scripted_chat(lambda t: t[0].upper()), "sous_chef", log, "s:x")
    prompts = [f"p{i % 5}" for i in range(60)]

    def worker(chunk):
        for p in chunk:
            chat("s:x", _user(p))

    threads = [threading.Thread(target=worker, args=(prompts[i::3],)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    calls = log.calls
    assert [c["seq"] for c in calls] == list(range(60))
    for i in range(5):
        mine = [c for c in calls if c["request"]["messages"][0]["content"] == f"p{i}"]
        assert sorted(c["occurrence"] for c in mine) == list(range(12))
        assert {c["response"]["content"] for c in mine} == {f"P{i}"}


def test_replay_log_serves_by_key_and_explains_mismatches():
    log = CallLog()
    chat = RecordingChat(scripted_chat(lambda t: "R:" + t[-1]), "chef", log, "s:x")
    chat("s:x", _user("plan for Household: 2 people"))
    chat("s:x", _user("plan for Household: 2 people"))
    replay = ReplayLog(log.calls)
    chef = ReplayChat("chef", replay, "s:x")
    reply = chef("s:x", _user("plan for Household: 2 people"))
    assert reply.text == "R:plan for Household: 2 people"
    assert reply.input_tokens is not None and reply.finish_reason == "stop"
    with pytest.raises(ReplayLeftover, match="1 recorded call"):
        replay.check()
    chef("s:x", _user("plan for Household: 2 people"))
    replay.check()

    with pytest.raises(ReplayMismatch, match="recorded 2 time"):
        chef("s:x", _user("plan for Household: 2 people"))
    with pytest.raises(ReplayMismatch) as info:
        chef("s:x", _user("plan for Household: 4 people"))
    msg = str(info.value)
    assert "'chef'" in msg
    assert "-plan for Household: 2 people" in msg
    assert "+plan for Household: 4 people" in msg
    with pytest.raises(ReplayMismatch, match="no 'sous_chef' calls"):
        ReplayChat("sous_chef", replay)("s:x", _user("anything"))
    with pytest.raises(ReplayMismatch):
        replay.check()  # the swallowed mismatches resurface


def test_replay_reraises_a_recorded_model_error():
    msgs = canonical_messages(_user("q"))
    calls = [{"seq": 0, "role": "chef", "occurrence": 0, "request_sha256": request_sha256(msgs),
              "request": {"messages": msgs}, "model": {},
              "response": None, "error": {"type": "TimeoutError", "message": "slow"},
              "latency_s": 1.0}]
    with pytest.raises(ReplayedLLMError, match="TimeoutError: slow"):
        ReplayChat("chef", ReplayLog(calls))("s:x", _user("q"))


def test_roles_are_the_pipelines():
    assert recorder.ROLES == ("chef", "sous_chef")
    with pytest.raises(ValueError, match="unknown role"):
        RecordingChat(lambda *a, **k: None, "nutritionist", CallLog(), "s:x")


def test_content_recipe_ids_are_deterministic_and_unique():
    from app.agents.llm_output import GeneratedRecipe

    r = GeneratedRecipe.model_validate({"name": "A", "ingredients": [
        {"name": "x", "deal_id": 1, "quantity": "1", "unit": "u"}], "instructions": ["c {0}"]})
    a, b = ContentRecipeIds(), ContentRecipeIds()
    first = [a("sous_chef_1", r), a("sous_chef_1", r), a("sous_chef_2", r)]
    assert first == [b("sous_chef_1", r), b("sous_chef_1", r), b("sous_chef_2", r)]
    assert len(set(first)) == 3


def test_production_recipe_ids_stay_random():
    from app.agents.plan_pipeline import PlanDeps
    from app.agents.recipe_build import random_recipe_id

    assert PlanDeps().recipe_ids is random_recipe_id
    assert random_recipe_id("c", None) != random_recipe_id("c", None)


def test_diff_values():
    assert diff_values({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == []
    out = diff_values({"a": [1, {"b": 2}], "t": 1}, {"a": [1, {"b": 3}, 4], "t": True})
    assert [d.path for d in out] == ["$.a[1].b", "$.a[2]", "$.t"]
    assert diff_values({"x": [{"seconds": 1}]}, {"x": [{"seconds": 2}]},
                       ignore=[("x", "*", "seconds")]) == []


# --------------------------------------------------------------------------
# Record -> replay through the real pipeline
# --------------------------------------------------------------------------

ROUND_TRIPS = [(s, r) for s in SNAPSHOTS for r in (("vegetarian",), ())]


@pytest.mark.parametrize("snap,restrictions", ROUND_TRIPS,
                         ids=[f"{s}-{'+'.join(r) or 'open'}" for s, r in ROUND_TRIPS])
def test_record_replay_round_trip_on_each_snapshot(snap, restrictions):
    recorded = record(scenario(snap, restrictions))
    state = recorded["final_state"]
    assert state["status"] == "completed", state["errors"]
    assert len(state["approved_recipe_ids"]) >= 1
    assert recorded["state_notes"] == []  # the result is plain JSON
    assert {c["role"] for c in recorded["llm_calls"]} == {"chef", "sous_chef"}
    assert recorded["progress"][:2] == ["confirm_selection", "plan_dishes"]
    assert recorded["progress"][-1] == "finalize_meal_plan"
    assert recorded["tracker_calls"][0] == "start_run"
    assert state["dish_plan"]["dishes"]

    result = runner.replay(json.loads(dump_artifact(recorded)))
    assert result.ok, "\n".join(map(str, result.differences))
    assert result.replayed["artifact"]["mode"] == "replay"
    assert result.replayed["final_state"]["saved_recipe_ids"] == state["saved_recipe_ids"]


def test_a_snapshot_without_anchors_is_refused_up_front():
    from app.agents.plan_pipeline import SelectionError
    with pytest.raises(SelectionError, match="anchor"):
        record(scenario("sparse5", ()))


def test_recording_is_deterministic_with_deterministic_models():
    a, b = record(scenario()), record(scenario())
    assert compare_artifacts(a, b) == []
    assert sorted(a["final_state"]["generated_recipes"]) == \
        sorted(b["final_state"]["generated_recipes"])


def test_recording_captures_retries_tokens_and_identity():
    art = record(scenario("week-2026-10-16", ()))
    sous = [c for c in art["llm_calls"] if c["role"] == "sous_chef"]
    retries = [c for c in sous if len(c["request"]["messages"]) == 3]
    assert retries, "scripted SousChef should have forced a validate-and-retry"
    assert [m["type"] for m in retries[0]["request"]["messages"]] == \
        ["user", "assistant", "user"]
    assert all(c["response"]["usage"]["total_tokens"] > 0 for c in sous)
    assert art["llm"]["chef"]["provider"] == "scripted"
    assert art["llm"]["chef"]["model"] == "chef-script"
    assert "scripted_chat" in art["llm"]["chef"]["class"]
    assert art["snapshot"]["name"] == "week-2026-10-16"
    assert len(art["snapshot"]["content_sha256"]) == 64
    assert art["code"]["git_sha"] is None or len(art["code"]["git_sha"]) == 40


def test_parallel_sous_chef_replay_is_independent_of_scheduling(monkeypatch):
    """Random delays before every replayed call shuffle the SousChef threads'
    order; each must still get its own recorded reply."""
    recorded = record(scenario("week-2026-10-16"))
    rng = random.Random(1234)
    real = ReplayChat.__call__
    lock = threading.Lock()

    def jittery(self, spec, messages, **params):
        with lock:
            delay = rng.random() * 0.02
        time.sleep(delay)
        return real(self, spec, messages, **params)

    monkeypatch.setattr(ReplayChat, "__call__", jittery)
    for _ in range(6):
        result = runner.replay(copy.deepcopy(recorded))
        assert result.ok, "\n".join(map(str, result.differences))


def test_call_log_order_is_irrelevant_to_replay():
    """The log's order is wall-clock start order and is never used to match:
    reversing the SousChef entries (whose replies are all distinct) changes
    nothing."""
    recorded = record(scenario())
    sous = [c for c in recorded["llm_calls"] if c["role"] == "sous_chef"
            and c["response"]["content"].startswith("{")]
    assert len({c["response"]["content"] for c in sous}) == len(sous) >= 2
    shuffled = copy.deepcopy(recorded)
    others = [c for c in shuffled["llm_calls"] if c["role"] != "sous_chef"]
    moved = list(reversed([c for c in shuffled["llm_calls"] if c["role"] == "sous_chef"]))
    shuffled["llm_calls"] = [dict(c, seq=i) for i, c in enumerate(moved + others)]
    assert runner.replay(shuffled).ok


def test_request_mismatch_fails_loudly():
    recorded = record(scenario())
    changed = copy.deepcopy(recorded)
    changed["scenario"]["user"]["household_size"] = 4
    with pytest.raises(ReplayMismatch) as info:
        runner.replay(changed)
    msg = str(info.value)
    assert "'chef'" in msg
    assert "-- Household: 2 people" in msg and "+- Household: 4 people" in msg


def test_leftover_calls_fail_strict_replay_only():
    recorded = record(scenario())
    extra = copy.deepcopy(recorded)
    msgs = canonical_messages(_user("never asked"))
    extra["llm_calls"].append({
        "seq": len(extra["llm_calls"]) + 100, "role": "sous_chef",
        "request_sha256": request_sha256(msgs), "occurrence": 0,
        "request": {"messages": msgs}, "model": extra["llm"]["sous_chef"],
        "response": {"content": "{}", "usage": None, "response_metadata": {}},
        "error": None, "latency_s": 0.0})
    with pytest.raises(ReplayLeftover, match="1 recorded call"):
        runner.replay(copy.deepcopy(extra))
    lenient = runner.replay(extra, strict=False)
    assert lenient.ok
    assert [c["request"]["messages"] for c in lenient.unused] == [msgs]


def test_changed_response_shows_up_as_a_state_difference():
    recorded = record(scenario())
    tampered = copy.deepcopy(recorded)
    call = next(c for c in tampered["llm_calls"] if c["role"] == "sous_chef"
                and c["response"]["content"].startswith("{"))
    body = json.loads(call["response"]["content"])
    body["estimated_prep_time"] = 999
    call["response"]["content"] = json.dumps(body)
    result = runner.replay(tampered)
    assert not result.ok
    # Recipe ids are content-derived, so the changed recipe arrives under a
    # new id: the difference is visible wherever the reply lands.
    assert any("999" in json.dumps(d.replayed) for d in result.differences
               if d.replayed is not None and not isinstance(d.replayed, runner._Missing))


def test_snapshot_drift_is_refused():
    recorded = record(scenario())
    recorded["snapshot"]["content_sha256"] = "0" * 64
    with pytest.raises(SnapshotDrift, match="has changed"):
        runner.replay(recorded)


def test_runner_guards_database_access_itself(monkeypatch):
    """Even without the test fixture's guard, a run cannot reach the DB."""
    from app.db.database import Database

    with runner.offline_guard():
        with pytest.raises(RuntimeError, match="database access"):
            Database().initialize()
    with pytest.raises(AssertionError, match="touched the database"):
        Database().initialize()


# --------------------------------------------------------------------------
# Artifact schema
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def committed():
    return json.loads(COMMITTED_ARTIFACT.read_text(encoding="utf-8"))


def test_committed_artifact_is_valid_and_canonical(committed):
    validate_artifact(committed)
    assert dump_artifact(committed) == COMMITTED_ARTIFACT.read_text(encoding="utf-8")
    assert committed["artifact"]["mode"] == "record"
    assert committed["artifact"]["format_version"] == runner.ARTIFACT_FORMAT_VERSION
    assert committed["final_state"]["status"] == "completed"


def test_committed_artifact_replays_to_its_final_state(committed):
    result = runner.replay(COMMITTED_ARTIFACT)
    assert result.ok, "\n".join(map(str, result.differences))
    assert result.replayed["replayed_from"]["created_at"] == committed["artifact"]["created_at"]


def _set(path, value):
    def mutate(d):
        target = d
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return mutate


def _del(path):
    def mutate(d):
        target = d
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
    return mutate


@pytest.mark.parametrize("mutate,match", [
    (_set(("artifact", "format_version"), 1), "format_version"),
    (_set(("artifact", "format_version"), True), "format_version"),
    (_set(("artifact", "kind"), "other"), "kind"),
    (_set(("artifact", "mode"), "dream"), "mode"),
    (_del(("final_state",)), "missing field"),
    (_set(("extra",), 1), "unexpected field"),
    (_del(("llm", "chef")), "roles"),
    (_set(("snapshot", "format_version"), 9), "snapshot.format_version"),
    (_set(("scenario", "user", "num_meals"), 0), "scenario"),
    (_set(("llm_calls", 0, "role"), "nutritionist"), "role"),
    (_set(("llm_calls", 0, "occurrence"), 1), "occurrences"),
    (_set(("llm_calls", 0, "seq"), 10_000), "seq"),
    (_set(("llm_calls", 0, "request", "messages", 0, "content"), "edited"), "request_sha256"),
    (_set(("llm_calls", 0, "error"), {"type": "X", "message": "y"}), "exactly one"),
    (_del(("llm_calls", 0, "latency_s")), "missing field"),
    (_set(("llm_calls", 0, "latency_s"), True), "latency_s"),
])
def test_malformed_artifacts_are_refused(committed, mutate, match):
    data = copy.deepcopy(committed)
    mutate(data)
    with pytest.raises(ArtifactError, match=match):
        validate_artifact(data)


def test_duplicate_call_key_is_refused(committed):
    data = copy.deepcopy(committed)
    data["llm_calls"].append(dict(copy.deepcopy(data["llm_calls"][0]), seq=10_000))
    with pytest.raises(ArtifactError, match="duplicate call key"):
        validate_artifact(data)


def test_bad_artifact_file(tmp_path):
    (tmp_path / "x.json").write_text("{nope")
    with pytest.raises(ArtifactError, match="not valid JSON"):
        load_artifact(tmp_path / "x.json")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_replay_of_the_committed_artifact(tmp_path, capsys):
    out = tmp_path / "replayed.json"
    assert run_eval.main(["replay", "--artifact", str(COMMITTED_ARTIFACT),
                          "--out", str(out)]) == 0
    assert "replay matches" in capsys.readouterr().out
    assert load_artifact(out)["artifact"]["mode"] == "replay"


def test_cli_replay_reports_differences(tmp_path, committed, capsys):
    tampered = copy.deepcopy(committed)
    call = next(c for c in tampered["llm_calls"] if c["role"] == "sous_chef"
                and c["response"]["content"].startswith("{"))
    body = json.loads(call["response"]["content"])
    body["estimated_prep_time"] = 777
    call["response"]["content"] = json.dumps(body)
    path = tmp_path / "t.json"
    path.write_text(dump_artifact(tampered))
    assert run_eval.main(["replay", "--artifact", str(path)]) == 1
    assert "estimated_prep_time" in capsys.readouterr().out


def test_cli_record_with_scripted_models(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "default_chat_factory", scripted_factory)
    out = tmp_path / "rec.json"
    assert run_eval.main(["record", "--scenario", str(SCENARIOS), "--out", str(out)]) == 0
    recorded = load_artifact(out)
    assert recorded["final_state"]["status"] == "completed"
    assert compare_artifacts(load_artifact(COMMITTED_ARTIFACT), recorded) == []


def test_cli_record_needs_a_name_for_a_multi_scenario_file(tmp_path):
    one = scenario().to_dict()
    path = tmp_path / "s.json"
    path.write_text(json.dumps([one, dict(one, name="b")]))
    with pytest.raises(SystemExit):
        run_eval.main(["record", "--scenario", str(path), "--out", str(tmp_path / "o.json")])


# --------------------------------------------------------------------------
# Fixture regeneration
# --------------------------------------------------------------------------

def regenerate() -> None:
    (sc,) = load_scenarios(SCENARIOS)
    from app.eval.runner import write_artifact
    write_artifact(runner.record(sc, chat_factory=scripted_factory), COMMITTED_ARTIFACT)
    print(f"wrote {COMMITTED_ARTIFACT}")


if __name__ == "__main__":
    regenerate()
