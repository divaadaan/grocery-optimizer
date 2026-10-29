"""Evaluation runner with record/replay (ROADMAP Track E §3).

Deterministic and offline: "recording" uses scripted chat models (real
langchain ``BaseChatModel``s whose replies are computed from the request), and
replay serves from call logs. Any database or MLflow access fails the test.

Regenerate the committed replay fixture (only when the graph's prompts or
state legitimately change -- the diff is the review)::

    ~/projects/grocery-optimizer/.venv/bin/python -m app.tests.test_eval_runner
"""
import copy
import json
import random
import threading
import time
from pathlib import Path
from typing import Callable, List

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.eval import recorder, runner
from app.eval.recorder import (
    CallLog,
    ContentRecipeIds,
    RecordingLLM,
    ReplayLeftover,
    ReplayLLM,
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
from app.tests.test_eval_snapshots import _chef_reply, _json_after
from scripts import run_eval

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eval_runs"
SCENARIOS = FIXTURES / "scenarios.json"
COMMITTED_ARTIFACT = FIXTURES / "seed27-vegetarian.json"
SNAPSHOTS = ("seed27", "week-2026-10-16", "sparse5")
POSTAL = "M5V3A8"


@pytest.fixture(autouse=True)
def no_database_no_mlflow(monkeypatch):
    """Anything that reaches Postgres or MLflow fails the test."""
    from app.db.database import Database
    from app.services import mlflow_logger

    def no_db(*args, **kwargs):
        raise AssertionError("eval runner test touched the database")

    def no_mlflow():
        raise AssertionError("eval runner test touched MLflow")

    monkeypatch.setattr(Database, "initialize", no_db)
    monkeypatch.setattr(Database, "get_connection", no_db)
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", no_mlflow)


# --------------------------------------------------------------------------
# Scripted "live" models
# --------------------------------------------------------------------------

class ConversationModel(BaseChatModel):
    """A real langchain chat model whose reply is computed from the whole
    request (so it can answer a retry differently from a first attempt).
    ``usage`` adds provider-style token counts to the reply."""

    respond: Callable[[List[str]], str]
    usage: bool = False

    @property
    def _llm_type(self) -> str:
        return "scripted-conversation"

    @property
    def _identifying_params(self):
        return {"model": "scripted", "usage": self.usage}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        texts = [m.content for m in messages]
        content = self.respond(texts)
        extra = {}
        if self.usage:
            n_in = sum(len(t) for t in texts) // 4
            n_out = len(content) // 4
            extra["usage_metadata"] = {"input_tokens": n_in, "output_tokens": n_out,
                                       "total_tokens": n_in + n_out}
        message = AIMessage(content=content, **extra)
        return ChatResult(generations=[ChatGeneration(message=message)])


ASSIGNED = "**Your Assigned Deals (cite each by its deal_id):**"
STAPLES = "**Pantry Staples Already On Hand (cite each by its product_id):**"


def sous_chef_reply(texts: List[str]) -> str:
    """One recipe citing every assigned deal plus the first staple. A group
    whose smallest deal id is even answers its *first* attempt with
    non-JSON, so the recording holds invoke_validated retries too."""
    prompt = texts[0]
    deals = _json_after(prompt, ASSIGNED)
    staples = _json_after(prompt, STAPLES)
    if len(texts) == 1 and min(d["deal_id"] for d in deals) % 2 == 0:
        return "Sure! Here is a recipe: (not JSON)"
    ingredients = [{"name": d.get("product_name") or "deal", "deal_id": d["deal_id"],
                    "quantity": "1", "unit": "unit"} for d in deals]
    if staples:
        ingredients.append({"name": staples[0]["name"], "product_id": staples[0]["product_id"],
                            "quantity": "1", "unit": "tsp"})
    recipe = {"name": "Scripted " + "+".join(str(d["deal_id"]) for d in deals),
              "meal_type": "dinner", "cuisine_type": "Any", "servings": 2,
              "estimated_prep_time": "20 minutes", "ingredients": ingredients,
              "instructions": ["Cook it."]}
    return json.dumps({"recipes": [recipe]})


def nutritionist_reply(texts: List[str]) -> str:
    """Facts that depend on the recipe, so a reply served to the wrong
    request would show up in the final state."""
    calories = 200 + len(texts[0]) % 700
    return json.dumps({"approved": False, "feedback": "advisory only",
                       "nutrition_facts": {"calories_per_serving": calories},
                       "health_score": str(40 + len(texts[0]) % 50)})


def scripted_factory(role, spec):
    return {
        "chef": lambda: ConversationModel(respond=lambda t: _chef_reply(t[0]), usage=True),
        "sous_chef": lambda: ConversationModel(respond=sous_chef_reply, usage=True),
        "nutritionist": lambda: ConversationModel(respond=nutritionist_reply),
    }[role]()


def scenario(snapshot="seed27", restrictions=("vegetarian",), **user) -> Scenario:
    base = {"postal_code": POSTAL, "budget": 80.0, "budget_period": "weekly",
            "household_size": 2, "num_meals": 3,
            "dietary_restrictions": list(restrictions)}
    base.update(user)
    return Scenario.from_dict({
        "name": f"{snapshot}-{'+'.join(restrictions) or 'open'}", "snapshot": snapshot,
        "user": base,
        "llm": {"chef": {"provider": "scripted", "model": "chef-script"},
                "sous_chef": {"provider": "scripted", "model": "sous-script"},
                "nutritionist": {"provider": "scripted", "model": "nutri-script"}}})


def record(sc: Scenario):
    return runner.record(sc, llm_factory=scripted_factory)


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------

def test_scenario_round_trips_and_fills_defaults():
    sc = Scenario.from_dict({"name": "x", "snapshot": "seed27",
                             "user": {"postal_code": POSTAL, "budget": 50,
                                      "household_size": 1, "num_meals": 2}})
    d = sc.to_dict()
    assert d["user"] == {"user_id": 1, "postal_code": POSTAL, "budget": 50.0,
                         "household_size": 1, "num_meals": 2, "dietary_restrictions": [],
                         "budget_period": "weekly", "budget_spent_this_period": 0.0,
                         "preferences": {}}
    assert d["llm"] == {} and d["notes"] is None
    assert Scenario.from_dict(d) == sc
    assert Scenario.from_dict(json.loads(json.dumps(d))).to_dict() == d


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d.update(colour="red"), "unexpected field"),
    (lambda d: d.pop("snapshot"), "missing field"),
    (lambda d: d["user"].pop("budget"), "missing field"),
    (lambda d: d["user"].update(user_name="x"), "unexpected field"),
    (lambda d: d["user"].update(household_size="2"), "household_size"),
    (lambda d: d["user"].update(num_meals=True), "num_meals"),
    (lambda d: d["user"].update(num_meals=0), "num_meals"),
    (lambda d: d["user"].update(budget_period="week"), "budget_period"),
    (lambda d: d["user"].update(dietary_restrictions=["vegan", 3]), "dietary_restrictions"),
    (lambda d: d["llm"].update(sommelier={"provider": "a", "model": "b"}), "unknown role"),
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


# --------------------------------------------------------------------------
# Recorder / replayer units
# --------------------------------------------------------------------------

def test_canonical_request_hash_is_content_only():
    a = canonical_messages([HumanMessage(content="hi"), AIMessage(content="x")])
    assert a == [{"type": "human", "content": "hi"}, {"type": "ai", "content": "x"}]
    assert canonical_messages("hi") == canonical_messages([HumanMessage(content="hi")])
    assert request_sha256(a) == request_sha256(json.loads(json.dumps(a)))
    assert request_sha256(a) != request_sha256(a[:1])


def test_recorder_logs_usage_only_when_reported_and_records_errors():
    log = CallLog()
    with_usage = RecordingLLM(ConversationModel(respond=lambda t: "{}", usage=True),
                              "chef", log, provider="p", model="m")
    without = RecordingLLM(ConversationModel(respond=lambda t: "{}"), "nutritionist", log)
    with_usage.invoke([HumanMessage(content="abcdefgh")])
    without.invoke([HumanMessage(content="abcdefgh")])

    class Boom:
        def invoke(self, messages):
            raise ConnectionError("model server down")

    failing = RecordingLLM(Boom(), "sous_chef", log)
    with pytest.raises(ConnectionError):
        failing.invoke([HumanMessage(content="q")])

    first, second, third = log.calls
    assert first["response"]["usage"] == {"input_tokens": 2, "output_tokens": 0,
                                          "total_tokens": 2}
    assert first["model"]["provider"] == "p" and first["model"]["model"] == "m"
    assert first["model"]["params"] == {"model": "scripted", "usage": True}
    assert second["response"]["usage"] is None  # not reported -> null, not estimated
    assert third["response"] is None
    assert third["error"] == {"type": "ConnectionError", "message": "model server down"}
    assert [c["latency_s"] >= 0 for c in log.calls] == [True] * 3


def test_wrappers_expose_only_invoke():
    rec = RecordingLLM(ConversationModel(respond=lambda t: "{}"), "chef", CallLog())
    rep = ReplayLLM("chef", ReplayLog([]))
    for wrapper in (rec, rep):
        with pytest.raises(AttributeError, match="wraps only"):
            wrapper.with_structured_output
        with pytest.raises(AttributeError, match="wraps only"):
            wrapper.bind(stop=["x"])
        with pytest.raises(TypeError):
            wrapper.invoke([HumanMessage(content="q")], stop=["x"])


def test_recorder_is_thread_safe_and_counts_occurrences():
    log = CallLog()
    llm = RecordingLLM(ConversationModel(respond=lambda t: t[0].upper()), "sous_chef", log)
    prompts = [f"p{i % 5}" for i in range(60)]

    def worker(chunk):
        for p in chunk:
            llm.invoke([HumanMessage(content=p)])

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
    llm = RecordingLLM(ConversationModel(respond=lambda t: "R:" + t[-1]), "chef", log)
    llm.invoke([HumanMessage(content="plan for Household Size: 2 people")])
    llm.invoke([HumanMessage(content="plan for Household Size: 2 people")])
    replay = ReplayLog(log.calls)
    chef = ReplayLLM("chef", replay)
    assert chef.invoke([HumanMessage(content="plan for Household Size: 2 people")]).content \
        == "R:plan for Household Size: 2 people"
    with pytest.raises(ReplayLeftover, match="1 recorded call"):
        replay.check()
    chef.invoke([HumanMessage(content="plan for Household Size: 2 people")])
    replay.check()

    with pytest.raises(ReplayMismatch, match="recorded 2 time"):
        chef.invoke([HumanMessage(content="plan for Household Size: 2 people")])
    with pytest.raises(ReplayMismatch) as info:
        chef.invoke([HumanMessage(content="plan for Household Size: 4 people")])
    msg = str(info.value)
    assert "'chef'" in msg
    assert "-plan for Household Size: 2 people" in msg
    assert "+plan for Household Size: 4 people" in msg
    with pytest.raises(ReplayMismatch, match="no 'sous_chef' calls"):
        ReplayLLM("sous_chef", replay).invoke("anything")
    with pytest.raises(ReplayMismatch):
        replay.check()  # the swallowed mismatches resurface


def test_replay_reraises_a_recorded_model_error():
    calls = [{"seq": 0, "role": "chef", "occurrence": 0,
              "request_sha256": request_sha256(canonical_messages("q")),
              "request": {"messages": canonical_messages("q")}, "model": {},
              "response": None, "error": {"type": "TimeoutError", "message": "slow"},
              "latency_s": 1.0}]
    with pytest.raises(ReplayedLLMError, match="TimeoutError: slow"):
        ReplayLLM("chef", ReplayLog(calls)).invoke("q")


def test_content_recipe_ids_are_deterministic_and_unique():
    from app.agents.llm_output import GeneratedRecipe

    r = GeneratedRecipe.model_validate({"name": "A", "ingredients": [
        {"name": "x", "deal_id": 1, "quantity": "1", "unit": "u"}], "instructions": ["c"]})
    a, b = ContentRecipeIds(), ContentRecipeIds()
    first = [a("sous_chef_1", r), a("sous_chef_1", r), a("sous_chef_2", r)]
    assert first == [b("sous_chef_1", r), b("sous_chef_1", r), b("sous_chef_2", r)]
    assert len(set(first)) == 3


def test_production_recipe_ids_stay_random():
    from app.agents.deps import GraphDeps
    from app.agents.sous_chef import SousChef, random_recipe_id

    assert GraphDeps.__dataclass_fields__["recipe_ids"].default is None
    chef = SousChef(llm=object())
    assert chef.recipe_id is random_recipe_id
    assert chef.recipe_id("c", None) != chef.recipe_id("c", None)
    ids = ContentRecipeIds()
    assert SousChef(llm=object(), recipe_id=ids).recipe_id is ids


def test_diff_values():
    assert diff_values({"a": [1, {"b": 2}]}, {"a": [1, {"b": 2}]}) == []
    out = diff_values({"a": [1, {"b": 2}], "t": 1}, {"a": [1, {"b": 3}, 4], "t": True})
    assert [d.path for d in out] == ["$.a[1].b", "$.a[2]", "$.t"]
    assert diff_values({"x": [{"timestamp": 1}]}, {"x": [{"timestamp": 2}]},
                       ignore=[("x", "*", "timestamp")]) == []


# --------------------------------------------------------------------------
# Record -> replay through the real graph
# --------------------------------------------------------------------------

ROUND_TRIPS = [(s, r) for s in SNAPSHOTS for r in (("vegetarian",), ())]


@pytest.mark.parametrize("snap,restrictions", ROUND_TRIPS,
                         ids=[f"{s}-{'+'.join(r) or 'open'}" for s, r in ROUND_TRIPS])
def test_record_replay_round_trip_on_each_snapshot(snap, restrictions):
    recorded = record(scenario(snap, restrictions))
    state = recorded["final_state"]
    assert state["status"] == "completed", state["errors"]
    assert len(state["approved_recipe_ids"]) >= 1
    assert recorded["state_notes"] == []  # today's state is plain JSON
    roles = {c["role"] for c in recorded["llm_calls"]}
    assert roles == {"chef", "sous_chef", "nutritionist"}
    assert recorded["progress"][:3] == ["initialize_chef", "load_recipe_catalog",
                                        "plan_ingredient_groups"]
    assert recorded["progress"].count("sous_chef_generate") == 3
    assert recorded["tracker_calls"][0] == "start_run"

    result = runner.replay(json.loads(dump_artifact(recorded)))
    assert result.ok, "\n".join(map(str, result.differences))
    assert result.replayed["artifact"]["mode"] == "replay"
    assert result.replayed["final_state"]["saved_recipe_ids"] == state["saved_recipe_ids"]


def test_recording_is_deterministic_with_deterministic_models():
    a, b = record(scenario()), record(scenario())
    assert compare_artifacts(a, b) == []
    assert sorted(a["final_state"]["generated_recipes"]) == \
        sorted(b["final_state"]["generated_recipes"])


def test_recording_captures_retries_tokens_and_identity():
    art = record(scenario())
    sous = [c for c in art["llm_calls"] if c["role"] == "sous_chef"]
    retries = [c for c in sous if len(c["request"]["messages"]) == 3]
    assert retries, "scripted SousChef should have forced an invoke_validated retry"
    assert [m["type"] for m in retries[0]["request"]["messages"]] == ["human", "ai", "human"]
    assert all(c["response"]["usage"]["total_tokens"] > 0 for c in sous)
    assert all(c["response"]["usage"] is None
               for c in art["llm_calls"] if c["role"] == "nutritionist")
    assert art["llm"]["chef"]["provider"] == "scripted"
    assert art["llm"]["chef"]["model"] == "chef-script"
    assert art["llm"]["chef"]["class"].endswith("ConversationModel")
    assert art["snapshot"]["name"] == "seed27"
    assert len(art["snapshot"]["content_sha256"]) == 64
    assert art["code"]["git_sha"] is None or len(art["code"]["git_sha"]) == 40


def test_parallel_sous_chef_replay_is_independent_of_scheduling(monkeypatch):
    """Random delays before every replayed call shuffle the three SousChef
    threads' order; each must still get its own recorded reply."""
    recorded = record(scenario("week-2026-10-16"))
    rng = random.Random(1234)
    real = ReplayLLM.invoke
    lock = threading.Lock()

    def jittery(self, messages, *args, **kwargs):
        with lock:
            delay = rng.random() * 0.02
        time.sleep(delay)
        return real(self, messages, *args, **kwargs)

    monkeypatch.setattr(ReplayLLM, "invoke", jittery)
    orders = set()
    for _ in range(6):
        result = runner.replay(copy.deepcopy(recorded))
        assert result.ok, "\n".join(map(str, result.differences))
        orders.add(tuple(c["request_sha256"] for c in result.replayed["llm_calls"]
                         if c["role"] == "sous_chef"))
    # Not asserted (scheduling may happen to repeat), but expected: the
    # serve order varied while the outcome did not.
    print(f"{len(orders)} distinct SousChef serve orders across 6 replays")


def test_call_log_order_is_irrelevant_to_replay():
    """The log's order is wall-clock start order and is never used to match:
    reversing the SousChef entries (whose replies are all distinct, so a
    positional replay would hand each chef another chef's reply) changes
    nothing."""
    recorded = record(scenario())
    sous = [c for c in recorded["llm_calls"] if c["role"] == "sous_chef"
            and c["response"]["content"].startswith("{")]
    assert len({c["response"]["content"] for c in sous}) == len(sous) == 3
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
    assert "-- Household Size: 2 people" in msg and "+- Household Size: 4 people" in msg


def test_leftover_calls_fail_strict_replay_only():
    recorded = record(scenario())
    extra = copy.deepcopy(recorded)
    msgs = canonical_messages("never asked")
    extra["llm_calls"].append({
        "seq": len(extra["llm_calls"]) + 100, "role": "nutritionist",
        "request_sha256": request_sha256(msgs), "occurrence": 0,
        "request": {"messages": msgs}, "model": extra["llm"]["nutritionist"],
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
    call = next(c for c in tampered["llm_calls"] if c["role"] == "nutritionist")
    body = json.loads(call["response"]["content"])
    body["nutrition_facts"]["calories_per_serving"] = 9999
    call["response"]["content"] = json.dumps(body)
    result = runner.replay(tampered)
    assert not result.ok
    assert any(d.path.endswith(".nutrition_facts.calories_per_serving")
               and d.replayed == 9999 for d in result.differences)


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
    # Restored afterwards (to this test's own guard).
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
    (_set(("artifact", "format_version"), 2), "format_version"),
    (_set(("artifact", "format_version"), True), "format_version"),
    (_set(("artifact", "kind"), "other"), "kind"),
    (_set(("artifact", "mode"), "dream"), "mode"),
    (_del(("final_state",)), "missing field"),
    (_set(("extra",), 1), "unexpected field"),
    (_del(("llm", "chef")), "roles"),
    (_set(("snapshot", "format_version"), 9), "snapshot.format_version"),
    (_set(("scenario", "user", "num_meals"), 0), "scenario"),
    (_set(("llm_calls", 0, "role"), "sommelier"), "role"),
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
    call = next(c for c in tampered["llm_calls"] if c["role"] == "nutritionist")
    body = json.loads(call["response"]["content"])
    body["health_score"] = "1"
    call["response"]["content"] = json.dumps(body)
    path = tmp_path / "t.json"
    path.write_text(dump_artifact(tampered))
    assert run_eval.main(["replay", "--artifact", str(path)]) == 1
    assert "health_score" in capsys.readouterr().out


def test_cli_record_with_scripted_models(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "default_llm_factory", scripted_factory)
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
    write_artifact(runner.record(sc, llm_factory=scripted_factory), COMMITTED_ARTIFACT)
    print(f"wrote {COMMITTED_ARTIFACT}")


if __name__ == "__main__":
    regenerate()
