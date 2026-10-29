"""Run one evaluation scenario through the real recipe graph, recording or
replaying its LLM calls, and write a run artifact.

A run is: a :class:`Scenario` (snapshot name + an in-memory user profile +
optional per-role model choice) -> :class:`GraphDeps` built from the frozen
snapshot (``app.eval.snapshots``) and wrapped chat models
(``app.eval.recorder``) -> ``run_recipe_generation`` -> a run artifact.

* ``record`` wraps real chat models in :class:`RecordingLLM`. It needs a live
  model (the default factory is ``llm_factory.make_llm``); tests pass scripted
  fakes through ``llm_factory=``.
* ``replay`` re-runs the artifact's scenario with :class:`ReplayLLM` serving
  the recorded responses -- no model, no network -- and compares the new
  final state with the recorded one (:func:`compare_artifacts`).

Neither mode touches the database: deals and products come only from the
snapshot, recipes go to an ``InMemoryRecipeStore``, the tracker is a
:class:`NullTracker`, and while the graph runs ``Database.initialize`` /
``Database.get_connection`` and MLflow's configuration are replaced with
functions that raise (:func:`offline_guard`).

Scenario format (JSON or YAML; one object, a list, or ``{"scenarios": [...]}``)
------------------------------------------------------------------------------
::

    {
      "name": "veg-2p-3meals",               # unique within a scenario set
      "snapshot": "seed27",                  # committed name or path
      "user": {
        "user_id": 1,                         # default 1; nothing is looked up
        "postal_code": "M5V3A8",
        "budget": 80.0,                       # period budget (users.budget)
        "budget_period": "weekly",            # "weekly" | "monthly"
        "budget_spent_this_period": 0.0,
        "household_size": 2,
        "dietary_restrictions": ["vegetarian"],
        "num_meals": 3,
        "preferences": {}
      },
      "llm": {                                # optional; omitted role -> settings
        "chef": {"provider": "ollama", "model": "llama3.1:8b"}
      },
      "notes": null
    }

These are exactly ``run_recipe_generation``'s inputs, minus the callback and
the deps. Unknown keys are refused.

Run artifact (``artifact.format_version`` 1) -- what scorers (E4) read
-----------------------------------------------------------------------
A JSON object, written with sorted keys::

    artifact      {kind: "recipe-eval-run", format_version: 1,
                   mode: "record" | "replay", created_at: ISO UTC}
    code          {git_sha: str|null, git_dirty: bool|null}
    scenario      the Scenario, normalised (defaults filled in)
    snapshot      {name, format_version, as_of, captured_at, content_sha256}
                  -- content_sha256 is over the canonical snapshot text; a
                  replay refuses a snapshot whose content has changed
    llm           role -> {provider, model, class, params} of the model used
                  (for replay: the recorded identities, copied)
    timing        {started_at, wall_seconds, llm_seconds}
    progress      node names in completion order, as ``progress_callback``
                  saw them (one "sous_chef_generate" per parallel task)
    llm_calls     ordered by seq (start order); each:
                    seq, role, request_sha256, occurrence,
                    request {messages: [{type, content, ...}]},
                    model {provider, model, class, params},
                    response {content, usage{input_tokens, output_tokens,
                              total_tokens} | null, response_metadata} | null,
                    error {type, message} | null,
                    latency_s
                  Matching key is (role, request_sha256, occurrence) -- see
                  app/eval/recorder.py for why.
    final_state   the graph's final RecipeGenerationState as JSON
                  (``recorder.jsonable``)
    state_notes   [{path, note}] for every lossy conversion in final_state
                  (empty for today's state: everything in it is plain JSON)
    tracker_calls tracker method names, in order (no MLflow is touched)
    replayed_from null, or for a replay {created_at, git_sha, mode} of the
                  artifact it replayed

Volatile state: ``agent_call_log[*].timestamp`` / ``.duration`` are
wall-clock and are ignored by :func:`compare_artifacts`
(:data:`VOLATILE_STATE_PATHS`). Everything else in the final state is
reproduced exactly by replay; recipe ids are made deterministic with
``recorder.ContentRecipeIds``.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as _dt
import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from app.eval.recorder import (
    ROLES,
    CallLog,
    ContentRecipeIds,
    RecordingLLM,
    ReplayLLM,
    ReplayLog,
    call_key,
    jsonable,
    request_sha256,
)
from app.eval.snapshots import FORMAT_VERSION as SNAPSHOT_FORMAT_VERSION
from app.eval.snapshots import Snapshot, dump_snapshot, load_snapshot

#: Bump on any change to the artifact layout; :func:`validate_artifact`
#: refuses other versions.
ARTIFACT_FORMAT_VERSION = 1
ARTIFACT_KIND = "recipe-eval-run"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Final-state paths that are wall-clock and never compared ("*" = any key
#: or index).
VOLATILE_STATE_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("agent_call_log", "*", "timestamp"),
    ("agent_call_log", "*", "duration"),
)


class ScenarioError(ValueError):
    """A scenario is malformed."""


class ArtifactError(ValueError):
    """A run artifact is malformed, of an unknown version, or inconsistent."""


class SnapshotDrift(ArtifactError):
    """The snapshot a replay loads is not the one the artifact recorded."""


# --------------------------------------------------------------------------
# Scenario
# --------------------------------------------------------------------------

def _periods() -> Tuple[str, ...]:
    from app.services.budget import PERIODS
    return PERIODS


@dataclass(frozen=True)
class RoleModel:
    provider: str
    model: str


@dataclass(frozen=True)
class UserProfile:
    postal_code: str
    budget: float
    household_size: int
    num_meals: int
    dietary_restrictions: Tuple[str, ...] = ()
    budget_period: str = "weekly"
    budget_spent_this_period: float = 0.0
    preferences: Dict[str, Any] = field(default_factory=dict)
    user_id: int = 1

    def run_kwargs(self) -> Dict[str, Any]:
        """Keyword arguments for ``run_recipe_generation``."""
        return {
            "user_id": self.user_id, "postal_code": self.postal_code,
            "budget": self.budget, "household_size": self.household_size,
            "dietary_restrictions": list(self.dietary_restrictions),
            "num_meals": self.num_meals,
            "preferences": copy.deepcopy(self.preferences),
            "budget_period": self.budget_period,
            "budget_spent_this_period": self.budget_spent_this_period,
        }


_USER_FIELDS = {
    # name: (types, required)
    "user_id": ((int,), False),
    "postal_code": ((str,), True),
    "budget": ((int, float), True),
    "budget_period": ((str,), False),
    "budget_spent_this_period": ((int, float), False),
    "household_size": ((int,), True),
    "dietary_restrictions": ((list,), False),
    "num_meals": ((int,), True),
    "preferences": ((dict,), False),
}


def _check_fields(where: str, data: Any, spec: Dict[str, Tuple[tuple, bool]]) -> None:
    if not isinstance(data, dict):
        raise ScenarioError(f"{where}: expected an object, got {type(data).__name__}")
    unexpected = sorted(set(data) - set(spec))
    if unexpected:
        raise ScenarioError(f"{where}: unexpected field(s) {unexpected}")
    missing = sorted(k for k, (_, req) in spec.items() if req and k not in data)
    if missing:
        raise ScenarioError(f"{where}: missing field(s) {missing}")
    for key, (types, _) in spec.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, types):
            raise ScenarioError(f"{where}.{key}: expected {types[0].__name__}, "
                                f"got {type(value).__name__}")


@dataclass(frozen=True)
class Scenario:
    """One evaluation run's inputs. Serialisable: :meth:`to_dict` /
    :meth:`from_dict` round-trip."""

    name: str
    snapshot: str
    user: UserProfile
    llm: Dict[str, RoleModel] = field(default_factory=dict)
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "Scenario":
        _check_fields("scenario", data, {
            "name": ((str,), True), "snapshot": ((str,), True), "user": ((dict,), True),
            "llm": ((dict, type(None)), False), "notes": ((str, type(None)), False)})
        if not data["name"].strip() or not data["snapshot"].strip():
            raise ScenarioError("scenario: name and snapshot must be non-empty")
        name = data["name"]
        user = data["user"]
        _check_fields(f"scenario {name!r}.user", user, _USER_FIELDS)
        restrictions = user.get("dietary_restrictions", [])
        if not all(isinstance(r, str) and r for r in restrictions):
            raise ScenarioError(f"scenario {name!r}.user.dietary_restrictions: "
                                "expected a list of non-empty strings")
        period = user.get("budget_period", "weekly")
        if period not in _periods():
            raise ScenarioError(f"scenario {name!r}.user.budget_period: {period!r} not in "
                                f"{_periods()}")
        for key in ("household_size", "num_meals"):
            if user[key] < 1:
                raise ScenarioError(f"scenario {name!r}.user.{key}: must be >= 1")
        if user["budget"] < 0 or user.get("budget_spent_this_period", 0) < 0:
            raise ScenarioError(f"scenario {name!r}.user: budget figures must be >= 0")
        profile = UserProfile(
            user_id=user.get("user_id", 1), postal_code=user["postal_code"],
            budget=float(user["budget"]), budget_period=period,
            budget_spent_this_period=float(user.get("budget_spent_this_period", 0.0)),
            household_size=user["household_size"],
            dietary_restrictions=tuple(restrictions), num_meals=user["num_meals"],
            preferences=copy.deepcopy(user.get("preferences", {})))

        llm = {}
        for role, spec in (data.get("llm") or {}).items():
            if role not in ROLES:
                raise ScenarioError(f"scenario {name!r}.llm: unknown role {role!r}; "
                                    f"known: {list(ROLES)}")
            _check_fields(f"scenario {name!r}.llm.{role}", spec,
                          {"provider": ((str,), True), "model": ((str,), True)})
            llm[role] = RoleModel(provider=spec["provider"], model=spec["model"])
        return cls(name=name, snapshot=data["snapshot"], user=profile, llm=llm,
                   notes=data.get("notes"))

    def to_dict(self) -> Dict[str, Any]:
        user = asdict(self.user)
        user["dietary_restrictions"] = list(self.user.dietary_restrictions)
        return {"name": self.name, "snapshot": self.snapshot, "user": user,
                "llm": {r: asdict(m) for r, m in sorted(self.llm.items())},
                "notes": self.notes}


def load_scenarios(path: Path | str) -> List[Scenario]:
    """Scenarios from a JSON or YAML file: one object, a list, or
    ``{"scenarios": [...]}``. Names must be unique."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    if isinstance(data, dict) and set(data) == {"scenarios"}:
        data = data["scenarios"]
    items = data if isinstance(data, list) else [data]
    scenarios = [Scenario.from_dict(item) for item in items]
    names = [s.name for s in scenarios]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ScenarioError(f"{path}: duplicate scenario name(s) {dupes}")
    return scenarios


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

class NullTracker:
    """``GraphDeps.tracker``: MLflowLogger's interface, recorded by method
    name only, no I/O. ``start_run`` returns ``""`` as a disabled MLflow does."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append(name)
            return "" if name == "start_run" else None
        return record


@contextlib.contextmanager
def offline_guard() -> Iterator[None]:
    """While the graph runs, any database or MLflow access raises.

    A belt over the braces of ``GraphDeps``: if a future node bypasses its
    injected dependency, an evaluation run fails instead of reading (or
    writing) Neon.
    """
    from app.db.database import Database
    from app.services import mlflow_logger

    def no_db(*args, **kwargs):
        raise RuntimeError("evaluation run attempted database access")

    def no_mlflow(*args, **kwargs):
        raise RuntimeError("evaluation run attempted to configure MLflow")

    saved = (Database.initialize, Database.get_connection, mlflow_logger._ensure_configured)
    Database.initialize = no_db
    Database.get_connection = no_db
    mlflow_logger._ensure_configured = no_mlflow
    try:
        yield
    finally:
        Database.initialize, Database.get_connection, mlflow_logger._ensure_configured = saved


@dataclass
class _Execution:
    final_state: Dict[str, Any]
    state_notes: List[Dict[str, str]]
    progress: List[str]
    tracker_calls: List[str]
    started_at: str
    wall_seconds: float


def _execute(scenario: Scenario, snapshot: Snapshot, llms: Dict[str, Any]) -> _Execution:
    from app import main_recipe_generation

    tracker = NullTracker()
    deps = snapshot.graph_deps(
        chef_llm=llms["chef"], sous_chef_llm=llms["sous_chef"],
        nutritionist_llm=llms["nutritionist"], tracker=tracker,
        recipe_ids=ContentRecipeIds())
    progress: List[str] = []
    started_at = _utc_now()
    t0 = time.perf_counter()
    with offline_guard():
        final = main_recipe_generation.run_recipe_generation(
            **scenario.user.run_kwargs(), progress_callback=progress.append,
            graph_deps=deps)
    wall = time.perf_counter() - t0
    notes: List[Dict[str, str]] = []
    state = jsonable(dict(final), notes)
    return _Execution(state, notes, progress, list(tracker.calls), started_at, wall)


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def code_identity() -> Dict[str, Any]:
    """``{git_sha, git_dirty}`` of this checkout; ``None`` where git can't say."""
    def git(*args: str) -> Optional[str]:
        try:
            out = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                                 text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    sha = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"git_sha": sha.strip() if sha else None,
            "git_dirty": None if status is None else bool(status.strip())}


def snapshot_identity(snapshot: Snapshot) -> Dict[str, Any]:
    meta = snapshot.data["meta"]
    digest = hashlib.sha256(dump_snapshot(snapshot.data).encode("utf-8")).hexdigest()
    return {"name": meta["name"], "format_version": meta["format_version"],
            "as_of": meta["as_of"], "captured_at": meta["captured_at"],
            "content_sha256": digest}


#: ``(role, RoleModel or None) -> chat model``. ``None``: use settings.
LLMFactory = Callable[[str, Optional[RoleModel]], Any]


def default_llm_factory(role: str, spec: Optional[RoleModel]) -> Any:
    """A real chat model from ``llm_factory.make_llm``. Needs a live provider."""
    from app.agents import llm_factory
    from app.config import settings

    if spec is None:
        return llm_factory.make_llm(role)
    setting = llm_factory.ROLES[role].model_setting
    cfg = settings.model_copy(update={"llm_provider": spec.provider, setting: spec.model})
    return llm_factory.make_llm(role, settings=cfg)


def _resolved_model(role: str, spec: Optional[RoleModel]) -> Tuple[str, str]:
    if spec is not None:
        return spec.provider, spec.model
    from app.agents import llm_factory
    from app.config import settings
    return settings.llm_provider, llm_factory.model_name(role)


def _artifact(mode: str, scenario: Scenario, snapshot: Snapshot, llm: Dict[str, Any],
              calls: List[Dict[str, Any]], run: _Execution,
              replayed_from: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    llm_seconds = sum(c["latency_s"] or 0.0 for c in calls)
    return {
        "artifact": {"kind": ARTIFACT_KIND, "format_version": ARTIFACT_FORMAT_VERSION,
                     "mode": mode, "created_at": _utc_now()},
        "code": code_identity(),
        "scenario": scenario.to_dict(),
        "snapshot": snapshot_identity(snapshot),
        "llm": llm,
        "timing": {"started_at": run.started_at, "wall_seconds": run.wall_seconds,
                   "llm_seconds": llm_seconds},
        "progress": run.progress,
        "llm_calls": calls,
        "final_state": run.final_state,
        "state_notes": run.state_notes,
        "tracker_calls": run.tracker_calls,
        "replayed_from": replayed_from,
    }


def record(scenario: Scenario, llm_factory: LLMFactory = default_llm_factory,
           snapshot: Optional[Snapshot] = None) -> Dict[str, Any]:
    """Run ``scenario`` against real (``llm_factory``-built) models, recording
    every call. Returns the artifact (validated)."""
    snapshot = snapshot or load_snapshot(scenario.snapshot)
    log = CallLog()
    wrapped = {}
    for role in ROLES:
        spec = scenario.llm.get(role)
        provider, model = _resolved_model(role, spec)
        wrapped[role] = RecordingLLM(llm_factory(role, spec), role, log,
                                     provider=provider, model=model)
    run = _execute(scenario, snapshot, wrapped)
    llm = {role: wrapped[role].identity for role in ROLES}
    return validate_artifact(_artifact("record", scenario, snapshot, llm, log.calls, run))


@dataclass
class ReplayResult:
    recorded: Dict[str, Any]
    replayed: Dict[str, Any]
    differences: List["Difference"]
    #: Recorded calls the replay never asked for (always empty when strict).
    unused: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.differences


def replay(artifact: Dict[str, Any] | Path | str, strict: bool = True) -> ReplayResult:
    """Re-run a recorded artifact from its call log and compare.

    Raises :class:`~app.eval.recorder.ReplayMismatch` if the run asks for a
    call the log does not hold (or, ``strict``, leaves one unused), and
    :class:`SnapshotDrift` if the snapshot is not the recorded one.
    Differences in the outcome are *returned*, not raised.
    """
    recorded = load_artifact(artifact) if not isinstance(artifact, dict) else \
        validate_artifact(artifact)
    scenario = Scenario.from_dict(recorded["scenario"])
    snapshot = load_snapshot(scenario.snapshot)
    now = snapshot_identity(snapshot)
    if now["content_sha256"] != recorded["snapshot"]["content_sha256"]:
        raise SnapshotDrift(
            f"snapshot {scenario.snapshot!r} has changed since this artifact was recorded "
            f"(content_sha256 {recorded['snapshot']['content_sha256'][:12]}... recorded, "
            f"{now['content_sha256'][:12]}... now); re-record rather than replay")

    log = ReplayLog(recorded["llm_calls"], strict=strict)
    run = _execute(scenario, snapshot, {role: ReplayLLM(role, log) for role in ROLES})
    log.check()
    served = [dict(copy.deepcopy(e), seq=i) for i, e in enumerate(log.served)]
    replayed = validate_artifact(_artifact(
        "replay", scenario, snapshot, copy.deepcopy(recorded["llm"]), served, run,
        replayed_from={"created_at": recorded["artifact"]["created_at"],
                       "git_sha": recorded["code"]["git_sha"],
                       "mode": recorded["artifact"]["mode"]}))
    return ReplayResult(recorded, replayed,
                        compare_artifacts(recorded, replayed, compare_calls=strict),
                        unused=log.unused())


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Difference:
    path: str
    recorded: Any
    replayed: Any

    def __str__(self) -> str:
        def short(v: Any) -> str:
            text = json.dumps(v, sort_keys=True, ensure_ascii=False, default=repr)
            return text if len(text) <= 160 else text[:160] + "..."
        return f"{self.path}: recorded {short(self.recorded)} != replayed {short(self.replayed)}"


class _Missing:
    """Stands in for an absent dict key / list element in a :class:`Difference`."""

    def __repr__(self) -> str:
        return "<missing>"


MISSING = _Missing()


def _volatile(path: Tuple[str, ...], patterns: Sequence[Tuple[str, ...]]) -> bool:
    return any(len(p) == len(path) and all(a == "*" or a == b for a, b in zip(p, path))
               for p in patterns)


def diff_values(a: Any, b: Any, path: Tuple[str, ...] = (),
                ignore: Sequence[Tuple[str, ...]] = ()) -> List[Difference]:
    """Every leaf where ``a`` and ``b`` differ. Dicts compare by key (order
    ignored), lists by position; paths matching ``ignore`` are skipped."""
    if _volatile(path, ignore):
        return []
    if isinstance(a, dict) and isinstance(b, dict):
        out: List[Difference] = []
        for key in list(a) + [k for k in b if k not in a]:
            out += diff_values(a.get(key, MISSING), b.get(key, MISSING),
                               path + (key,), ignore)
        return out
    if isinstance(a, list) and isinstance(b, list):
        out = []
        for i in range(max(len(a), len(b))):
            out += diff_values(a[i] if i < len(a) else MISSING,
                               b[i] if i < len(b) else MISSING, path + (str(i),), ignore)
        return out
    # True == 1 in Python, not in JSON.
    if a is MISSING or b is MISSING or a != b or isinstance(a, bool) != isinstance(b, bool):
        label = "$" + "".join(f"[{p}]" if p.isdigit() else f".{p}" for p in path)
        return [Difference(label, a, b)]
    return []


def compare_artifacts(recorded: Dict[str, Any], replayed: Dict[str, Any],
                      compare_calls: bool = True) -> List[Difference]:
    """What a replay changed: the final state (minus
    :data:`VOLATILE_STATE_PATHS`), the progress stages, and (``compare_calls``)
    the set of calls made, by matching key -- their order is thread
    scheduling and is not compared."""
    out = [Difference("final_state" + d.path[1:], d.recorded, d.replayed)
           for d in diff_values(recorded["final_state"], replayed["final_state"],
                                ignore=VOLATILE_STATE_PATHS)]
    if recorded["progress"] != replayed["progress"]:
        out.append(Difference("progress", recorded["progress"], replayed["progress"]))
    keys_a = sorted(call_key(c) for c in recorded["llm_calls"])
    keys_b = sorted(call_key(c) for c in replayed["llm_calls"])
    if compare_calls and keys_a != keys_b:
        out.append(Difference("llm_calls[keys]", [list(k) for k in keys_a],
                              [list(k) for k in keys_b]))
    return out


# --------------------------------------------------------------------------
# Artifact files and validation
# --------------------------------------------------------------------------

_TOP = {"artifact": dict, "code": dict, "scenario": dict, "snapshot": dict, "llm": dict,
        "timing": dict, "progress": list, "llm_calls": list, "final_state": dict,
        "state_notes": list, "tracker_calls": list, "replayed_from": (dict, type(None))}
_CALL = {"seq": int, "role": str, "request_sha256": str, "occurrence": int,
         "request": dict, "model": dict, "response": (dict, type(None)),
         "error": (dict, type(None)), "latency_s": (int, float, type(None))}
_RESPONSE = {"content": object, "usage": (dict, type(None)), "response_metadata": dict}
_SNAPSHOT = {"name": str, "format_version": int, "as_of": str, "captured_at": str,
             "content_sha256": str}
_MODEL = {"provider": (str, type(None)), "model": (str, type(None)), "class": str,
          "params": (dict, type(None))}


def _shape(where: str, data: Any, spec: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise ArtifactError(f"{where}: expected an object")
    missing = sorted(set(spec) - set(data))
    if missing:
        raise ArtifactError(f"{where}: missing field(s) {missing}")
    unexpected = sorted(set(data) - set(spec))
    if unexpected:
        raise ArtifactError(f"{where}: unexpected field(s) {unexpected}")
    for key, types in spec.items():
        value = data[key]
        if isinstance(value, bool) and types in (int, (int, float, type(None))):
            raise ArtifactError(f"{where}.{key}: expected a number, got bool")
        if not isinstance(value, types):
            raise ArtifactError(f"{where}.{key}: wrong type {type(value).__name__}")


def validate_artifact(data: Any) -> Dict[str, Any]:
    """Check a run artifact; return it unchanged. Raises :class:`ArtifactError`.

    Beyond shape: the version is known, every call's ``request_sha256`` is
    the hash of its recorded request, ``(role, request_sha256, occurrence)``
    is unique and each request's occurrences count 0, 1, ... with no gap,
    and the scenario parses.
    """
    if not isinstance(data, dict):
        raise ArtifactError("artifact: expected a JSON object")
    head = data.get("artifact")
    if not isinstance(head, dict) or head.get("kind") != ARTIFACT_KIND:
        raise ArtifactError(f"artifact.kind: expected {ARTIFACT_KIND!r}")
    version = head.get("format_version")
    if version != ARTIFACT_FORMAT_VERSION or isinstance(version, bool):
        raise ArtifactError(f"artifact.format_version: unsupported {version!r} "
                            f"(this reader reads {ARTIFACT_FORMAT_VERSION})")
    _shape("artifact", data, _TOP)
    _shape("artifact.artifact", head, {"kind": str, "format_version": int, "mode": str,
                                       "created_at": str})
    if head["mode"] not in ("record", "replay"):
        raise ArtifactError(f"artifact.mode: {head['mode']!r}")
    _shape("artifact.code", data["code"], {"git_sha": (str, type(None)),
                                           "git_dirty": (bool, type(None))})
    _shape("artifact.snapshot", data["snapshot"], _SNAPSHOT)
    if data["snapshot"]["format_version"] != SNAPSHOT_FORMAT_VERSION:
        raise ArtifactError("artifact.snapshot.format_version: "
                            f"{data['snapshot']['format_version']!r} is not "
                            f"{SNAPSHOT_FORMAT_VERSION}")
    _shape("artifact.timing", data["timing"], {"started_at": str,
                                               "wall_seconds": (int, float),
                                               "llm_seconds": (int, float)})
    if sorted(data["llm"]) != sorted(ROLES):
        raise ArtifactError(f"artifact.llm: expected roles {sorted(ROLES)}")
    for role, ident in data["llm"].items():
        _shape(f"artifact.llm.{role}", ident, _MODEL)
    try:
        Scenario.from_dict(data["scenario"])
    except ScenarioError as e:
        raise ArtifactError(f"artifact.scenario: {e}") from e
    if not all(isinstance(p, str) for p in data["progress"]):
        raise ArtifactError("artifact.progress: expected node names")

    seen = set()
    seqs = []
    occurrences: Dict[Tuple[str, str], List[int]] = {}
    for i, call in enumerate(data["llm_calls"]):
        where = f"artifact.llm_calls[{i}]"
        _shape(where, call, _CALL)
        if call["role"] not in ROLES:
            raise ArtifactError(f"{where}.role: unknown {call['role']!r}")
        _shape(f"{where}.model", call["model"], _MODEL)
        _shape(f"{where}.request", call["request"], {"messages": list})
        if (call["response"] is None) == (call["error"] is None):
            raise ArtifactError(f"{where}: exactly one of response / error must be set")
        if call["response"] is not None:
            _shape(f"{where}.response", call["response"], _RESPONSE)
        else:
            _shape(f"{where}.error", call["error"], {"type": str, "message": str})
        if request_sha256(call["request"]["messages"]) != call["request_sha256"]:
            raise ArtifactError(f"{where}: request_sha256 does not match the recorded request")
        key = call_key(call)
        if key in seen:
            raise ArtifactError(f"{where}: duplicate call key {key}")
        seen.add(key)
        seqs.append(call["seq"])
        occurrences.setdefault(key[:2], []).append(key[2])
    if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
        raise ArtifactError("artifact.llm_calls: seq must be unique and ascending")
    for (role, digest), occ in occurrences.items():
        if sorted(occ) != list(range(len(occ))):
            raise ArtifactError(f"artifact.llm_calls: {role} request {digest[:12]}... has "
                                f"occurrences {sorted(occ)}, expected 0..{len(occ) - 1}")
    return data


def dump_artifact(data: Dict[str, Any]) -> str:
    return json.dumps(validate_artifact(data), indent=1, sort_keys=True,
                      ensure_ascii=False, allow_nan=False) + "\n"


def write_artifact(data: Dict[str, Any], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_artifact(data), encoding="utf-8", newline="\n")
    return path


def load_artifact(path: Path | str) -> Dict[str, Any]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ArtifactError(f"{path}: not valid JSON: {e}") from e
    return validate_artifact(data)


__all__ = [
    "ARTIFACT_FORMAT_VERSION", "ArtifactError", "Difference", "NullTracker", "ReplayResult",
    "RoleModel", "Scenario", "ScenarioError", "SnapshotDrift", "UserProfile",
    "VOLATILE_STATE_PATHS", "compare_artifacts", "default_llm_factory",
    "diff_values", "dump_artifact", "load_artifact", "load_scenarios", "offline_guard",
    "record", "replay", "validate_artifact", "write_artifact",
]
