"""Deterministic scorers for recipe-evaluation runs (ROADMAP Track E §4).

Every scorer is a pure function ``(artifact, snapshot) -> dict`` over a run
artifact written by :mod:`app.eval.runner` and the frozen snapshot it ran
against (:mod:`app.eval.snapshots`). No database, no network, no LLM.
:func:`score_run` runs them all and returns a :class:`RunScore`.

The snapshot is the authority, not the run's own record: offered deals,
prices, staples and product attributes are all re-derived from it, and the
artifact's ``snapshot.content_sha256`` must match the snapshot's content
before anything is scored (:func:`verify_snapshot`).

The plan
--------
"The plan" is the run's approved recipes, ``final_state.approved_recipe_ids``
in order. Rejected and superseded recipes in ``generated_recipes`` count only
towards structural reliability and verdict agreement.

Scorers
-------
* :func:`score_structural` -- did the run complete with the requested number
  of approved recipes; per-role schema-validation retries and failures,
  reconstructed from ``llm_calls`` (see :func:`attempt_chains`); state errors.
* :func:`score_citations` -- every plan ingredient cites a deal that exists
  in the snapshot *and* was offered to this run (re-derived through the
  snapshot's dietary gate), or a confirmed ``assumed_on_hand`` staple.
* :func:`score_compliance` -- the hard gate. Recomputed from the snapshot's
  curated attributes with the production authority
  (:func:`app.agents.dietary.recipe_violations` over
  ``RESTRICTION_ATTRIBUTES`` / ``product_allows``), never from the run's own
  Nutritionist verdict, which is only compared against
  (``agreement``). NULL attributes fail closed. A restriction label the
  attribute map cannot express scores ``NA``, never ``PASS``. No ingredient
  or deal name is read.
* :func:`score_budget` -- plan cost vs the scenario's spend limit. A **lower
  bound**: one deal unit per citation, no quantity, household size or
  multi-buy semantics. Reported both as the per-recipe sum (what production's
  ``total_cost`` does) and per unique deal across the plan.
* :func:`score_on_sale` -- deal vs staple citations, deals used / offered,
  and savings vs regular price -- ``None`` (absent) when no used deal carries
  a regular price, never a fabricated 0.
* :func:`score_diversity` -- pairwise Jaccard of the plan's cited deal-id
  sets. Recipe-level and chef-independent on purpose: ``chef_id`` -> recipe
  assignment can swap on replay when two Chef groups are identical
  (``app/eval/recorder.py``).
* :func:`score_latency_tokens` -- per role and total, from ``llm_calls``.
  Unreported token counts stay ``None``; coverage is reported.

Verdict
-------
``FAIL`` if compliance fails or the run is structurally incomplete (not
``completed``, or fewer approved recipes than requested); otherwise ``NA`` if
any restriction could not be scored; otherwise ``PASS``. Budget, citations,
diversity etc. are reported, never gating.
"""
from __future__ import annotations

import inspect
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.eval.recorder import ROLES
from app.eval.runner import SnapshotDrift, snapshot_identity, validate_artifact
from app.eval.snapshots import Snapshot, SnapshotDeals

#: Bump on any change to the RunScore layout or to a metric's definition.
SCORE_SCHEMA_VERSION = 1

PASS, FAIL, NA = "PASS", "FAIL", "NA"

#: Cost equality tolerance (cents rounding in ``costing.price_recipe``).
_COST_EPS = 0.005

#: Prompt headers the SousChef prompts print the offered deals / staples
#: under (``app/agents/prompts.py``); used to rebuild the per-call citation
#: context when checking a last-attempt response. Tested against the templates.
ASSIGNED_DEALS_HEADER = "**Your Assigned Deals (cite each by its deal_id):**"
STAPLES_HEADER = "**Pantry Staples Already On Hand (cite each by its product_id):**"


class ScoringError(ValueError):
    """The artifact cannot be scored (e.g. its plan names a missing recipe)."""


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def verify_snapshot(artifact: Mapping[str, Any], snapshot: Snapshot) -> None:
    """Refuse to score against a snapshot other than the one the run used."""
    recorded = artifact["snapshot"]
    now = snapshot_identity(snapshot)
    if now["content_sha256"] != recorded["content_sha256"]:
        raise SnapshotDrift(
            f"snapshot {snapshot.name!r} content_sha256 {now['content_sha256'][:12]}... "
            f"is not the {recorded['content_sha256'][:12]}... this run recorded "
            f"({recorded['name']!r}); score against the recorded snapshot")


def _key(value: Any) -> Optional[str]:
    """An id as the string key every index here uses (as ``dietary._key``)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    return str(value).strip() or None


def _norm_label(label: Any) -> str:
    # The normalisation products.restrictions_to_attributes applies.
    return (label or "").strip().lower() if isinstance(label, str) else ""


@dataclass
class _View:
    """The run and its snapshot, indexed. Built per scorer call; cheap."""

    artifact: Mapping[str, Any]
    snapshot: Snapshot
    state: Mapping[str, Any]
    restrictions: List[str]
    plan_ids: List[str]
    plan: List[dict]
    deals: Dict[str, dict]
    products: Dict[str, dict]
    staples: Dict[str, dict]
    linked: Dict[str, List[dict]]
    offered: List[str]


def _view(artifact: Mapping[str, Any], snapshot: Snapshot) -> _View:
    verify_snapshot(artifact, snapshot)
    state = artifact["final_state"]
    user = artifact["scenario"]["user"]
    recipes = state.get("generated_recipes") or {}
    plan_ids = [str(r) for r in state.get("approved_recipe_ids") or []]
    missing = [r for r in plan_ids if r not in recipes]
    if missing:
        raise ScoringError(f"approved recipe(s) {missing} are not in generated_recipes")
    data = snapshot.data
    products = {str(p["product_id"]): p for p in data["products"]}
    staples = {k: p for k, p in products.items()
               if p["status"] == "confirmed" and p["assumed_on_hand"]}
    offered = SnapshotDeals(snapshot).fetch_current_deals(
        user["postal_code"], user.get("dietary_restrictions") or [])
    return _View(
        artifact=artifact, snapshot=snapshot, state=state,
        restrictions=list(user.get("dietary_restrictions") or []),
        plan_ids=plan_ids, plan=[recipes[r] for r in plan_ids],
        deals={str(d["deal_id"]): d for d in data["deals"]},
        products=products, staples=staples,
        linked={str(k): v for k, v in snapshot.linked_products().items()},
        offered=[str(d["deal_id"]) for d in offered])


def _ingredients(recipe: Mapping[str, Any]) -> List[Any]:
    return list(recipe.get("ingredients") or [])


def _cited_deal(ingredient: Any) -> Optional[str]:
    return _key(ingredient.get("deal_id")) if isinstance(ingredient, dict) else None


def _cited_product(ingredient: Any) -> Optional[str]:
    return _key(ingredient.get("product_id")) if isinstance(ingredient, dict) else None


def _recipe_deals(recipe: Mapping[str, Any]) -> List[str]:
    """Distinct cited deal ids, in first-citation order."""
    out: List[str] = []
    for ing in _ingredients(recipe):
        d = _cited_deal(ing)
        if d is not None and d not in out:
            out.append(d)
    return out


def _round(x: Optional[float], n: int = 2) -> Optional[float]:
    return None if x is None else round(x, n)


# --------------------------------------------------------------------------
# 1. Structural reliability
# --------------------------------------------------------------------------

def _max_attempts() -> int:
    from app.agents.llm_output import invoke_validated
    return inspect.signature(invoke_validated).parameters["max_attempts"].default


def _attempt(call: Mapping[str, Any]) -> int:
    """1-based attempt within ``invoke_validated``: the prompt, then one
    (bad reply, correction) pair per earlier attempt."""
    return sum(1 for m in call["request"]["messages"] if m.get("type") == "human")


def _msgs(messages: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps([[m.get("type"), m.get("content")] for m in messages],
                      sort_keys=True, ensure_ascii=False)


def attempt_chains(calls: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Mark each call ``terminal`` or not: a call is non-terminal when a later
    call of the same role continues its conversation (its messages, then this
    call's reply as an ``ai`` message, then a correction). Identical calls are
    matched one-for-one. Returns ``[{seq, role, attempt, terminal}]``."""
    continuations: Counter = Counter()
    for c in calls:
        m = c["request"]["messages"]
        if len(m) >= 3 and m[-2].get("type") == "ai":
            continuations[(c["role"], _msgs(m[:-2]), json.dumps(m[-2].get("content")))] += 1
    out = []
    for c in calls:
        reply = (c["response"] or {}).get("content") if c["error"] is None else None
        own = (c["role"], _msgs(c["request"]["messages"]), json.dumps(reply))
        continued = c["error"] is None and continuations[own] > 0
        if continued:
            continuations[own] -= 1
        out.append({"seq": c["seq"], "role": c["role"], "attempt": _attempt(c),
                    "terminal": not continued})
    return out


def _json_after(text: str, header: str) -> Any:
    start = text.index(header) + len(header)
    starts = [i for i in (text.find("[", start), text.find("{", start)) if i >= 0]
    value, _ = json.JSONDecoder().raw_decode(text, min(starts))
    return value


def _last_attempt_valid(call: Mapping[str, Any]) -> Optional[bool]:
    """Would ``invoke_validated`` have accepted this reply? ``None`` when it
    cannot be decided offline (a SousChef prompt whose offered ids can't be
    read back)."""
    from pydantic import ValidationError

    from app.agents import llm_output as lo

    try:
        data = json.loads(call["response"]["content"])
    except (TypeError, json.JSONDecodeError):
        return False
    schema = {"chef": lo.ChefPlan, "sous_chef": lo.RecipeBatch,
              "nutritionist": lo.NutritionistVerdict}[call["role"]]
    context = None
    if call["role"] == "sous_chef":
        prompt = call["request"]["messages"][0].get("content") or ""
        try:
            deals = _json_after(prompt, ASSIGNED_DEALS_HEADER)
            staples = _json_after(prompt, STAPLES_HEADER)
            context = lo.citation_context([d["deal_id"] for d in deals],
                                          [s["product_id"] for s in staples])
        except (ValueError, KeyError, TypeError):
            context = None
    try:
        schema.model_validate(data, context=context)
    except ValidationError:
        return False
    if call["role"] == "sous_chef" and context is None:
        return None
    return True


def score_structural(artifact: Mapping[str, Any], snapshot: Snapshot) -> Dict[str, Any]:
    v = _view(artifact, snapshot)
    state = v.state
    requested = artifact["scenario"]["user"]["num_meals"]
    approved = len(v.plan_ids)
    completed = state.get("status") == "completed"
    max_attempts = _max_attempts()
    calls = artifact["llm_calls"]
    chains = {c["seq"]: c for c in attempt_chains(calls)}

    roles = {}
    for role in ROLES:
        mine = [c for c in calls if c["role"] == role]
        stats = {"calls": len(mine), "invocations": 0, "retries": 0,
                 "retried_invocations": 0, "failed_invocations": 0,
                 "unverified_invocations": 0, "call_errors": 0}
        for c in mine:
            ch = chains[c["seq"]]
            if ch["attempt"] == 1:
                stats["invocations"] += 1
            else:
                stats["retries"] += 1
            if c["error"] is not None:
                stats["call_errors"] += 1
            if not ch["terminal"]:
                continue
            if ch["attempt"] > 1:
                stats["retried_invocations"] += 1
            if c["error"] is not None:
                stats["failed_invocations"] += 1
            elif ch["attempt"] >= max_attempts:
                ok = _last_attempt_valid(c)
                if ok is False:
                    stats["failed_invocations"] += 1
                elif ok is None:
                    stats["unverified_invocations"] += 1
        roles[role] = stats

    log = Counter(f"{e.get('agent')}:{e.get('action')}"
                  for e in state.get("agent_call_log") or [] if isinstance(e, dict))
    return {
        "status": state.get("status"),
        "completed": completed,
        "recipes_requested": requested,
        "recipes_approved": approved,
        "recipes_generated": len(state.get("generated_recipes") or {}),
        "recipes_rejected": len(state.get("rejected_recipe_ids") or []),
        "full_plan": completed and approved == requested,
        "iterations": state.get("iteration_count"),
        "max_attempts": max_attempts,
        "schema": roles,
        "agent_call_log": dict(sorted(log.items())),
        "errors": list(state.get("errors") or []),
        "warnings": list(state.get("warnings") or []),
    }


# --------------------------------------------------------------------------
# 2. Citation validity
# --------------------------------------------------------------------------

#: Citation classes; the first two are valid.
CITATION_KINDS = ("deal", "staple", "uncited", "double_cited", "dangling_deal",
                  "not_offered_deal", "dangling_product", "not_staple")


def classify_citation(ingredient: Any, v: _View) -> str:
    deal, product = _cited_deal(ingredient), _cited_product(ingredient)
    if deal is not None and product is not None:
        return "double_cited"
    if deal is not None:
        if deal not in v.deals:
            return "dangling_deal"
        return "deal" if deal in v.offered else "not_offered_deal"
    if product is not None:
        if product not in v.products:
            return "dangling_product"
        return "staple" if product in v.staples else "not_staple"
    return "uncited"


def score_citations(artifact: Mapping[str, Any], snapshot: Snapshot) -> Dict[str, Any]:
    v = _view(artifact, snapshot)
    counts = Counter({k: 0 for k in CITATION_KINDS})
    invalid = []
    for rid, recipe in zip(v.plan_ids, v.plan):
        for i, ing in enumerate(_ingredients(recipe)):
            kind = classify_citation(ing, v)
            counts[kind] += 1
            if kind not in ("deal", "staple"):
                invalid.append({"recipe_id": rid, "ingredient_index": i, "kind": kind,
                                "deal_id": _cited_deal(ing), "product_id": _cited_product(ing)})
    total = sum(counts.values())
    valid = counts["deal"] + counts["staple"]
    recorded = sorted({_key(d.get("deal_id")) for d in v.state.get("available_deals") or []
                       if _key(d.get("deal_id")) is not None}, key=_id_sort)
    return {
        "ingredients": total,
        "valid": valid,
        "invalid": total - valid,
        "validity_rate": _round(valid / total, 4) if total else None,
        "counts": dict(counts),
        "invalid_citations": invalid,
        "offered_deals": len(v.offered),
        # The run's own available_deals vs the snapshot gate's: a mismatch
        # means the run was not served what the snapshot says it should be.
        "offered_matches_run": recorded == sorted(v.offered, key=_id_sort),
    }


def _id_sort(k: str) -> Tuple[int, Any]:
    return (0, int(k)) if k.isdigit() else (1, k)


# --------------------------------------------------------------------------
# 3. Compliance (hard gate)
# --------------------------------------------------------------------------

def _citation_catalog(v: _View):
    from app.agents.dietary import CitationCatalog
    return CitationCatalog(deal_products=v.linked, staples=v.staples)


def _split_double_citations(recipe: Mapping[str, Any]) -> Dict[str, Any]:
    """An ingredient citing both a deal and a product is checked as both.

    ``dietary.ingredient_violations`` reads ``deal_id`` first and ignores a
    ``product_id`` beside it. Output validation forbids double citations, so
    production never sees one, but a scorer must not let the ignored half
    through: each half goes to the production check on its own.
    """
    out = []
    for ing in _ingredients(recipe):
        if _cited_deal(ing) is not None and _cited_product(ing) is not None:
            out.append({**ing, "product_id": None})
            out.append({**ing, "deal_id": None})
        else:
            out.append(ing)
    return {**recipe, "ingredients": out}


def score_compliance(artifact: Mapping[str, Any], snapshot: Snapshot) -> Dict[str, Any]:
    from app.agents.dietary import recipe_violations
    from app.services.products import (
        RESTRICTION_ATTRIBUTES,
        deal_allows,
        restrictions_to_attributes,
    )

    v = _view(artifact, snapshot)
    labels = [l for l in v.restrictions if _norm_label(l)]
    supported = [l for l in labels if _norm_label(l) in RESTRICTION_ATTRIBUTES]
    unsupported = [l for l in labels if _norm_label(l) not in RESTRICTION_ATTRIBUTES]
    catalog = _citation_catalog(v)

    def violations(recipe):
        if not supported:
            return []
        return [{"citation": x.citation, "attributes": list(x.attributes)}
                for x in recipe_violations(_split_double_citations(recipe),
                                           supported, catalog)]

    per_recipe = []
    for rid, recipe in zip(v.plan_ids, v.plan):
        found = violations(recipe)
        per_recipe.append({"recipe_id": rid, "compliant": not found, "violations": found})
    compliant = sum(r["compliant"] for r in per_recipe)

    # Agreement with the run's recorded (Nutritionist, deterministic) verdicts,
    # over every validated recipe, approved or not.
    recipes = v.state.get("generated_recipes") or {}
    disagreements, compared = [], 0
    for rid, result in sorted((v.state.get("validation_results") or {}).items()):
        if rid not in recipes or not isinstance(result, dict):
            continue
        compared += 1
        mine = not violations(recipes[rid])
        if bool(result.get("approved")) != mine:
            disagreements.append({"recipe_id": rid, "recorded": bool(result.get("approved")),
                                  "recomputed": mine})

    # Did the run serve a deal the gate should have withheld? (bug signal)
    leaked = sorted(
        {k for k in (_key(d.get("deal_id")) for d in v.state.get("available_deals") or [])
         if k is not None and not deal_allows(v.linked.get(k, []), labels)},
        key=_id_sort)

    if any(not r["compliant"] for r in per_recipe):
        gate = FAIL
    elif unsupported:
        gate = NA
    else:
        gate = PASS
    return {
        "gate": gate,
        "restrictions": labels,
        "supported_restrictions": supported,
        "unsupported_restrictions": unsupported,
        "required_attributes": list(restrictions_to_attributes(supported)) if supported else [],
        "unrestricted": not labels,
        "approved_recipes": len(per_recipe),
        "compliant_recipes": compliant,
        "compliance_rate": _round(compliant / len(per_recipe), 4) if per_recipe else None,
        "recipes": per_recipe,
        "agreement": {"compared": compared, "agree": compared - len(disagreements),
                      "disagreements": disagreements},
        "served_deals_failing_gate": leaked,
    }


# --------------------------------------------------------------------------
# 4. Budget (lower bound)
# --------------------------------------------------------------------------

BUDGET_CAVEATS = (
    "lower bound: one deal unit per cited ingredient",
    "ignores ingredient quantity and household size",
    "ignores per-item vs multi-buy pricing (deal_products.price_is_per_item)",
    "pantry staples cost 0 (assumed on hand)",
)


def score_budget(artifact: Mapping[str, Any], snapshot: Snapshot) -> Dict[str, Any]:
    v = _view(artifact, snapshot)
    user = artifact["scenario"]["user"]
    limit = float(user["budget"])
    spent = float(user.get("budget_spent_this_period") or 0.0)
    available = limit - spent

    per_recipe = []
    unpriced = 0
    for rid, recipe in zip(v.plan_ids, v.plan):
        cost = 0.0
        for d in _recipe_deals(recipe):
            if d in v.deals:
                cost += float(v.deals[d]["sale_price"])
            else:
                unpriced += 1
        per_recipe.append({"recipe_id": rid, "cost": round(cost, 2)})
    recipe_sum = round(sum(r["cost"] for r in per_recipe), 2)
    unique = {d for r in v.plan for d in _recipe_deals(r) if d in v.deals}
    unique_sum = round(sum(float(v.deals[d]["sale_price"]) for d in unique), 2)

    if unique_sum > available + _COST_EPS:
        status = "over"                       # over under either semantics
    elif recipe_sum > available + _COST_EPS:
        status = "over_per_recipe_sum_only"   # depends on the pending decision
    else:
        status = "within_lower_bound"         # not proof of being within
    recorded = v.state.get("total_cost")
    return {
        "lower_bound": True,
        "caveats": list(BUDGET_CAVEATS),
        "budget": limit,
        "budget_period": user.get("budget_period"),
        "spent_this_period": spent,
        "available": round(available, 2),
        "cost_per_recipe_sum": recipe_sum,
        "cost_per_unique_deal": unique_sum,
        "per_recipe": per_recipe,
        "unpriced_citations": unpriced,
        "status": status,
        "headroom_per_recipe_sum": round(available - recipe_sum, 2),
        "headroom_per_unique_deal": round(available - unique_sum, 2),
        "recorded_total_cost": recorded,
        "recorded_matches_per_recipe_sum": (
            None if not isinstance(recorded, (int, float))
            else abs(float(recorded) - recipe_sum) <= _COST_EPS),
    }


# --------------------------------------------------------------------------
# 5. On-sale use
# --------------------------------------------------------------------------

def score_on_sale(artifact: Mapping[str, Any], snapshot: Snapshot) -> Dict[str, Any]:
    v = _view(artifact, snapshot)
    deal_cites = staple_cites = 0
    for recipe in v.plan:
        for ing in _ingredients(recipe):
            d, p = _cited_deal(ing), _cited_product(ing)
            if d is not None and p is None:
                deal_cites += 1
            elif p is not None and d is None:
                staple_cites += 1
    cited = deal_cites + staple_cites
    used = sorted({d for r in v.plan for d in _recipe_deals(r) if d in v.offered},
                  key=_id_sort)
    known = [d for d in used if v.deals[d]["regular_price"] is not None]
    savings = (round(sum(max(float(v.deals[d]["regular_price"])
                             - float(v.deals[d]["sale_price"]), 0.0) for d in known), 2)
               if known else None)
    return {
        "deal_citations": deal_cites,
        "staple_citations": staple_cites,
        "deal_share": _round(deal_cites / cited, 4) if cited else None,
        "deals_used": len(used),
        "deals_offered": len(v.offered),
        "deal_utilisation": _round(len(used) / len(v.offered), 4) if v.offered else None,
        "used_deal_ids": [int(d) if d.isdigit() else d for d in used],
        # Secondary metric. None = unknown, never 0 (CLAUDE.md "Product framing").
        "savings_vs_regular": savings,
        "savings_basis": "per unique used deal: sum(max(regular_price - sale_price, 0))",
        "regular_price_known": len(known),
        "regular_price_coverage": f"regular price known for {len(known)}/{len(used)} deals used",
        "recorded_estimated_savings": v.state.get("estimated_savings"),
    }


# --------------------------------------------------------------------------
# 6. Diversity
# --------------------------------------------------------------------------

def score_diversity(artifact: Mapping[str, Any], snapshot: Snapshot) -> Dict[str, Any]:
    v = _view(artifact, snapshot)
    sets = sorted(((rid, set(_recipe_deals(r))) for rid, r in zip(v.plan_ids, v.plan)),
                  key=lambda t: t[0])
    pairs = []
    for (a, sa), (b, sb) in combinations(sets, 2):
        union = sa | sb
        pairs.append({"a": a, "b": b,
                      "jaccard": round(len(sa & sb) / len(union), 4) if union else None})
    defined = [p["jaccard"] for p in pairs if p["jaccard"] is not None]
    total = sum(len(s) for _, s in sets)
    return {
        "recipes": len(sets),
        "pairs": pairs,
        "mean_jaccard": round(sum(defined) / len(defined), 4) if defined else None,
        "max_jaccard": max(defined) if defined else None,
        "distinct_deals": len(set().union(*(s for _, s in sets))) if sets else 0,
        "deal_citations": total,
        "note": ("pairwise Jaccard of cited deal_id sets over the approved plan; "
                 "independent of recipe order and of chef assignment (not scored per "
                 "chef: chef_id -> recipe can swap on replay); undefined (null) for two "
                 "recipes citing no deal"),
    }


# --------------------------------------------------------------------------
# 7. Latency and tokens
# --------------------------------------------------------------------------

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


def _usage_block(calls: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    lat = [c["latency_s"] for c in calls if isinstance(c["latency_s"], (int, float))]
    tokens = {}
    for f in TOKEN_FIELDS:
        vals = [((c["response"] or {}).get("usage") or {}).get(f) for c in calls]
        vals = [x for x in vals if isinstance(x, int) and not isinstance(x, bool)]
        tokens[f] = {"sum": sum(vals) if vals else None, "known": len(vals)}
    known_total = tokens["total_tokens"]["known"]
    return {
        "calls": len(calls),
        "errors": sum(c["error"] is not None for c in calls),
        "latency_s": {"sum": _round(sum(lat), 6) if lat else None,
                      "mean": _round(sum(lat) / len(lat), 6) if lat else None,
                      "max": _round(max(lat), 6) if lat else None,
                      "known": len(lat)},
        "tokens": tokens,
        "token_coverage": f"tokens known for {known_total}/{len(calls)} calls",
    }


def score_latency_tokens(artifact: Mapping[str, Any], snapshot: Snapshot) -> Dict[str, Any]:
    verify_snapshot(artifact, snapshot)
    calls = artifact["llm_calls"]
    timing = artifact["timing"]
    return {
        "roles": {role: _usage_block([c for c in calls if c["role"] == role])
                  for role in ROLES},
        "total": _usage_block(calls),
        "wall_seconds": timing["wall_seconds"],
        "llm_seconds": timing["llm_seconds"],
    }


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------

@dataclass
class RunScore:
    """Every scorer's result for one run, plus the verdict. JSON-serialisable
    via :meth:`to_dict`; :meth:`summary_row` is one flat row for tables."""

    run: Dict[str, Any]
    verdict: str
    verdict_reasons: List[str]
    structural: Dict[str, Any]
    citations: Dict[str, Any]
    compliance: Dict[str, Any]
    budget: Dict[str, Any]
    on_sale: Dict[str, Any]
    diversity: Dict[str, Any]
    latency_tokens: Dict[str, Any]
    schema_version: int = SCORE_SCHEMA_VERSION
    kind: str = field(default="recipe-eval-score")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=1, sort_keys=True,
                          ensure_ascii=False, allow_nan=False)

    def summary_row(self) -> Dict[str, Any]:
        s, c, k, b = self.structural, self.citations, self.compliance, self.budget
        o, d, t = self.on_sale, self.diversity, self.latency_tokens["total"]
        return {
            "scenario": self.run["scenario"], "snapshot": self.run["snapshot"],
            "verdict": self.verdict, "status": s["status"],
            "recipes_approved": s["recipes_approved"],
            "recipes_requested": s["recipes_requested"],
            "schema_retries": sum(r["retries"] for r in s["schema"].values()),
            "schema_failures": sum(r["failed_invocations"] for r in s["schema"].values()),
            "compliance_gate": k["gate"], "compliance_rate": k["compliance_rate"],
            "verdict_disagreements": len(k["agreement"]["disagreements"]),
            "unsupported_restrictions": ",".join(k["unsupported_restrictions"]),
            "citation_validity": c["validity_rate"], "invalid_citations": c["invalid"],
            "cost_per_recipe_sum_lb": b["cost_per_recipe_sum"],
            "cost_per_unique_deal_lb": b["cost_per_unique_deal"],
            "budget_available": b["available"], "budget_status": b["status"],
            "deal_share": o["deal_share"], "deals_used": o["deals_used"],
            "deals_offered": o["deals_offered"], "savings_vs_regular": o["savings_vs_regular"],
            "mean_jaccard": d["mean_jaccard"], "max_jaccard": d["max_jaccard"],
            "llm_calls": t["calls"], "llm_seconds": t["latency_s"]["sum"],
            "total_tokens": t["tokens"]["total_tokens"]["sum"],
            "tokens_known_calls": t["tokens"]["total_tokens"]["known"],
        }


def _verdict(structural: Mapping[str, Any], compliance: Mapping[str, Any]) -> Tuple[str, List[str]]:
    reasons = []
    if compliance["gate"] == FAIL:
        bad = compliance["approved_recipes"] - compliance["compliant_recipes"]
        reasons.append(f"compliance: {bad}/{compliance['approved_recipes']} approved "
                       "recipe(s) violate the restrictions")
    if not structural["completed"]:
        reasons.append(f"structural: run status {structural['status']!r}, not 'completed'")
    elif not structural["full_plan"]:
        reasons.append(f"structural: {structural['recipes_approved']}/"
                       f"{structural['recipes_requested']} recipes approved")
    if reasons:
        return FAIL, reasons
    if compliance["gate"] == NA:
        return NA, [f"compliance: restriction(s) {compliance['unsupported_restrictions']} "
                    "cannot be expressed by product attributes; not scored"]
    return PASS, []


def score_run(artifact: Mapping[str, Any], snapshot: Optional[Snapshot] = None) -> RunScore:
    """Score one run. ``snapshot`` defaults to the scenario's snapshot by name;
    either way its content hash must match the artifact's."""
    from app.eval.snapshots import load_snapshot

    validate_artifact(artifact)
    if snapshot is None:
        snapshot = load_snapshot(artifact["scenario"]["snapshot"])
    verify_snapshot(artifact, snapshot)
    structural = score_structural(artifact, snapshot)
    compliance = score_compliance(artifact, snapshot)
    verdict, reasons = _verdict(structural, compliance)
    return RunScore(
        run={"scenario": artifact["scenario"]["name"],
             "snapshot": artifact["snapshot"]["name"],
             "snapshot_sha256": artifact["snapshot"]["content_sha256"],
             "mode": artifact["artifact"]["mode"],
             "created_at": artifact["artifact"]["created_at"],
             "git_sha": artifact["code"]["git_sha"],
             "models": {r: artifact["llm"][r].get("model") for r in ROLES}},
        verdict=verdict, verdict_reasons=reasons,
        structural=structural,
        citations=score_citations(artifact, snapshot),
        compliance=compliance,
        budget=score_budget(artifact, snapshot),
        on_sale=score_on_sale(artifact, snapshot),
        diversity=score_diversity(artifact, snapshot),
        latency_tokens=score_latency_tokens(artifact, snapshot),
    )


__all__ = [
    "ASSIGNED_DEALS_HEADER", "BUDGET_CAVEATS", "CITATION_KINDS", "FAIL", "NA", "PASS",
    "RunScore", "SCORE_SCHEMA_VERSION", "STAPLES_HEADER", "ScoringError",
    "attempt_chains", "classify_citation", "score_budget", "score_citations",
    "score_compliance", "score_diversity", "score_latency_tokens", "score_on_sale",
    "score_run", "score_structural", "verify_snapshot",
]
