"""Approach C generation: confirmed trays -> dish plan -> one recipe per dish.

``experiments/chef/DESIGN_APPROACH_C.md`` §2, §5-§8. Replaced the LangGraph
Chef / SousChef / Nutritionist pipeline (removed 2026-11-23); the API and the
eval harness both run this.

1. **Confirm.** Rebuild the trays from the user's *gated* deals and keep only
   what the user kept (ids no longer offered are dropped and reported, never
   trusted). At least one anchor must be kept.
2. **Plan.** One LLM call returns the dish-first plan; validated in code.
3. **Write.** One SousChef call per dish, in parallel, citing only that
   dish's deals, the stock-up deals and the confirmed pantry.
4. **Verdict.** ``app/agents/dietary.compliance_report`` over the cited
   products' curated attributes -- unchanged, deterministic.
5. **Retry.** A dish whose recipe failed validation or the verdict gets one
   more attempt, told why. An unreachable model stops all retries.
6. **Finalize.** ``completed`` (every dish), ``partial`` (>= 60%), else
   ``failed``; persisted with provenance (``recipe_generations``), costed for
   information only (budget-agnostic product), logged to MLflow.

The return value is a result dict shaped like the old graph's final state, so
``app/routes/recipes._build_generation_response`` and the job registry are
shared.
"""
from __future__ import annotations

import contextvars
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from app.agents import dish_planner, dish_writer, llm_chat
from app.agents.costing import deals_by_id, price_plan
from app.agents.dietary import CitationCatalog, compliance_report
from app.agents.dish_planner import DishPlan, PlannedDish
from app.agents.llm_errors import classify, INFRASTRUCTURE
from app.agents.recipe_build import RecipeIdFactory, random_recipe_id
from app.config import settings
from app.services import products, trays
from app.services.deal_labeller import Label
from app.services.mlflow_logger import MLflowLogger, generation_scope

logger = logging.getLogger(__name__)

PARTIAL_THRESHOLD = 0.6
MAX_PARALLEL_DISHES = 3

COMPLETED = "completed"
FAILED = "failed"


class SelectionError(ValueError):
    """The confirmed selection cannot be planned (a 422 for the API)."""


@dataclass
class Selection:
    """What the user confirmed, re-checked against freshly built trays."""
    proposal: trays.TrayProposal
    kept: Dict[str, List[trays.TrayItem]]
    pantry: List[Dict[str, Any]]
    dropped_deal_ids: List[int] = field(default_factory=list)
    dropped_pantry_ids: List[int] = field(default_factory=list)

    def kept_ids(self) -> List[int]:
        return [t.deal_id for name in trays.TRAYS for t in self.kept[name]]


def confirm_selection(proposal: trays.TrayProposal, kept_deal_ids: Optional[Iterable[int]],
                      pantry_product_ids: Optional[Iterable[int]]) -> Selection:
    """Intersect the client's choice with what the server offers now.

    ``None`` means "keep everything offered". An id the trays no longer offer
    (expired, re-ingested, or never offered) is dropped and reported -- the
    client can only narrow the server's offer, never add to it.
    """
    offered = proposal.tray_of()
    kept_set = set(offered) if kept_deal_ids is None else {int(i) for i in kept_deal_ids}
    kept = {name: [t for t in proposal.tray(name) if t.deal_id in kept_set]
            for name in trays.TRAYS}
    dropped = sorted(kept_set - set(offered))

    offered_pantry = {int(p["product_id"]): p for p in proposal.pantry}
    pantry_set = (set(offered_pantry) if pantry_product_ids is None
                  else {int(i) for i in pantry_product_ids})
    pantry = [p for pid, p in offered_pantry.items() if pid in pantry_set]
    dropped_pantry = sorted(pantry_set - set(offered_pantry))

    if not kept["anchors"]:
        raise SelectionError("Keep at least one anchor (protein) to plan dishes around.")
    return Selection(proposal=proposal, kept=kept, pantry=pantry,
                     dropped_deal_ids=dropped, dropped_pantry_ids=dropped_pantry)


class InMemoryRecipeStore:
    """A ``recipe_store`` that persists nothing.

    For runs that must not write the database -- tests, including the
    ``-m llm`` end-to-end test, and dry runs. Ids count up from 1 so they look
    like what the route expects in ``saved_recipe_ids``.

    Mirrors ``DatabaseService``: ``saved`` holds one entry per recipe (with
    its ``generation_id``), ``generations`` one provenance dict per generation
    keyed by ``generation_id`` -- the ``recipe_generations`` row.
    """

    def __init__(self) -> None:
        self.saved: List[Dict[str, Any]] = []
        self.generations: Dict[str, Dict[str, Any]] = {}

    def save_recipes(self, user_id: int, recipes: List[Dict],
                     generation: Optional[Dict[str, Any]] = None) -> List[int]:
        generation_id = (generation or {}).get("generation_id")
        if generation_id is not None:
            self.generations[generation_id] = {
                **generation, "user_id": user_id, "num_recipes_saved": len(recipes)}
        ids = []
        for recipe in recipes:
            recipe_id = len(self.saved) + 1
            self.saved.append({"id": recipe_id, "user_id": user_id,
                               "generation_id": generation_id, "recipe": recipe})
            ids.append(recipe_id)
        return ids

    def generation_recipes(self, generation_id: str) -> List[Dict[str, Any]]:
        """The recipes one generation saved, in save order."""
        return [e["recipe"] for e in self.saved if e["generation_id"] == generation_id]


@dataclass
class PlanDeps:
    """Everything external the pipeline touches; defaults are production."""
    fetch_deals: Optional[Callable[..., List[Dict[str, Any]]]] = None
    fetch_labels: Optional[Callable[[Iterable[int]], Dict[int, Label]]] = None
    fetch_pantry: Optional[Callable[[], List[Dict[str, Any]]]] = None
    fetch_deal_products: Optional[Callable[[Iterable[int]], Dict[str, List[Dict]]]] = None
    recipe_store: Any = None
    tracker: Any = MLflowLogger
    chat_fn: llm_chat.ChatFn = llm_chat.chat
    #: Per-role chat functions (the eval harness records each role); default chat_fn.
    planner_chat_fn: Optional[llm_chat.ChatFn] = None
    writer_chat_fn: Optional[llm_chat.ChatFn] = None
    planner_spec: Optional[str] = None
    writer_spec: Optional[str] = None
    recipe_ids: RecipeIdFactory = random_recipe_id
    #: Eval snapshot identity for provenance; None in production.
    snapshot: Optional[Dict[str, Any]] = None

    def resolved(self) -> "PlanDeps":
        from app.services import deal_labeller, recipe_catalog
        from app.services.database import DatabaseService

        db_service = None
        if self.fetch_deals is None or self.recipe_store is None:
            db_service = DatabaseService()
        return PlanDeps(
            fetch_deals=self.fetch_deals or db_service.fetch_gated_deals,
            fetch_labels=self.fetch_labels or deal_labeller.fetch_labels,
            fetch_pantry=self.fetch_pantry or recipe_catalog.fetch_pantry_staples,
            fetch_deal_products=self.fetch_deal_products or recipe_catalog.fetch_deal_products,
            recipe_store=self.recipe_store or db_service,
            tracker=self.tracker, chat_fn=self.chat_fn,
            planner_chat_fn=self.planner_chat_fn or self.chat_fn,
            writer_chat_fn=self.writer_chat_fn or self.chat_fn,
            planner_spec=self.planner_spec or settings.llm_planner_model,
            writer_spec=self.writer_spec or settings.llm_writer_model,
            recipe_ids=self.recipe_ids, snapshot=self.snapshot,
        )

    def models(self) -> Dict[str, Dict[str, str]]:
        out = {}
        for role, spec in (("chef", self.planner_spec), ("sous_chef", self.writer_spec)):
            # Provenance only: the chat call itself validates the spec.
            provider, _, name = (spec or "").partition(":")
            out[role] = {"provider": provider, "model": name}
        return out


def build_selection(postal_code: str, dietary_restrictions: Sequence[str],
                    kept_deal_ids: Optional[Iterable[int]],
                    pantry_product_ids: Optional[Iterable[int]], deps: PlanDeps
                    ) -> tuple:
    """``(Selection, gated deal rows, full pantry staple rows)``."""
    restrictions = list(dietary_restrictions or [])
    deals = deps.fetch_deals(postal_code, restrictions)
    labels = deps.fetch_labels(int(d["deal_id"]) for d in deals)
    proposal = trays.build_trays(deals, labels)
    staples = deps.fetch_pantry()
    proposal.pantry = trays.offered_pantry(staples, restrictions)
    selection = confirm_selection(proposal, kept_deal_ids, pantry_product_ids)
    return selection, deals, staples


@dataclass
class _DishOutcome:
    index: int
    dish: PlannedDish
    recipe: Optional[Dict[str, Any]] = None
    report: Optional[Dict[str, Any]] = None
    attempts: int = 0
    error: Optional[str] = None
    infrastructure: Optional[str] = None

    @property
    def approved(self) -> bool:
        return self.recipe is not None and bool(self.report and self.report["approved"])


def _usage(replies: Sequence[llm_chat.Reply]) -> Dict[str, int]:
    inp = sum(r.input_tokens or 0 for r in replies)
    out = sum(r.output_tokens or 0 for r in replies)
    return {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}


def run_plan_generation(*, user_id: int, postal_code: str, household_size: int,
                        dietary_restrictions: Sequence[str], num_meals: int,
                        preferences: Optional[Dict[str, Any]] = None,
                        kept_deal_ids: Optional[Iterable[int]] = None,
                        pantry_product_ids: Optional[Iterable[int]] = None,
                        deps: Optional[PlanDeps] = None,
                        generation_id: Optional[str] = None,
                        progress_callback: Optional[Callable[[str], None]] = None,
                        ) -> Dict[str, Any]:
    """Run one approach-C generation and return the result dict.

    Raises ``products.UnsupportedRestrictionError`` before any work for a
    restriction the app does not enforce, and :class:`SelectionError` when the
    confirmed selection has no anchor.
    """
    products.require_supported_restrictions(dietary_restrictions)
    deps = (deps or PlanDeps()).resolved()
    generation_id = generation_id or str(uuid.uuid4())
    restrictions = list(dietary_restrictions or [])
    preferences = preferences or {}

    def progress(node: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(node)
        except Exception as exc:  # noqa: BLE001 - progress must never fail a run
            logger.warning("progress_callback failed for %s: %s", node, exc)

    selection, deals, staples = build_selection(
        postal_code, restrictions, kept_deal_ids, pantry_product_ids, deps)
    progress("confirm_selection")

    run = _Run(deps=deps, user_id=user_id, postal_code=postal_code,
               household_size=household_size, restrictions=restrictions,
               num_meals=num_meals, preferences=preferences,
               generation_id=generation_id, selection=selection, deals=deals,
               staples=staples, progress=progress)
    with generation_scope(generation_id):
        return run.execute()


@dataclass
class _Run:
    deps: PlanDeps
    user_id: int
    postal_code: str
    household_size: int
    restrictions: List[str]
    num_meals: int
    preferences: Dict[str, Any]
    generation_id: str
    selection: Selection
    deals: List[Dict[str, Any]]
    staples: List[Dict[str, Any]]
    progress: Callable[[str], None]
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    call_log: List[Dict[str, Any]] = field(default_factory=list)
    infrastructure: List[str] = field(default_factory=list)
    plan: Optional[DishPlan] = None
    mlflow_run_id: str = ""

    # -------------------------------------------------------------- helpers

    def _log_call(self, agent: str, spec: str, replies: Sequence[llm_chat.Reply],
                  prompt: str, started: float, success: bool,
                  error: Optional[str] = None, order: tuple = (0, 0)) -> None:
        duration = time.monotonic() - started
        provider, _, name = (spec or "").partition(":")
        self.deps.tracker.log_agent_call(agent_name=agent, duration=duration, model=name,
                                         success=success, error=error,
                                         usage=_usage(replies), provider=provider)
        self.call_log.append({
            # (round, dish) -- the log is sorted by it, so parallel SousChefs
            # finishing in any order give the same log.
            "order": list(order),
            "agent": agent, "model": spec, "seconds": round(duration, 2),
            "success": success, "error": error, **_usage(replies),
            "truncated_prompt": any(r.truncated_prompt for r in replies),
            "prompt": prompt, "replies": [r.text for r in replies],
        })

    def _record_failure(self, agent: str, exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"[:500]
        if classify(exc) == INFRASTRUCTURE:
            self.infrastructure.append(f"{agent}: {text}")
        return text

    # -------------------------------------------------------------- stages

    def execute(self) -> Dict[str, Any]:
        sel = self.selection
        if sel.dropped_deal_ids:
            self.warnings.append(f"{len(sel.dropped_deal_ids)} kept deal(s) are no longer "
                                 "offered and were left out")
        if sel.dropped_pantry_ids:
            self.warnings.append(f"{len(sel.dropped_pantry_ids)} pantry item(s) are not "
                                 "available to you and were left out")
        self.mlflow_run_id = self.deps.tracker.start_run(
            user_id=self.user_id, num_meals=self.num_meals, budget=None,
            dietary_restrictions=self.restrictions, generation_id=self.generation_id,
            models=self.deps.models()) or ""

        if not self._plan():
            return self._fail(f"Planning failed: {'; '.join(self.errors)}")
        self.progress("plan_dishes")

        outcomes = self._write_all()
        approved = [o for o in outcomes if o.approved]
        self.progress("dietary_verdict")
        enough = approved and len(approved) >= self.num_meals * PARTIAL_THRESHOLD
        if not enough:
            reason = (f"Infrastructure failure: {self.infrastructure[0]} -- "
                      if self.infrastructure else "")
            return self._fail(f"{reason}Insufficient approved recipes: {len(approved)}/"
                              f"{self.num_meals} approved", outcomes)
        return self._finalize(outcomes)

    def _plan(self) -> bool:
        spec = self.deps.planner_spec
        started = time.monotonic()
        try:
            self.plan, replies, prompt = dish_planner.plan_dishes(
                self.selection.kept, self.selection.pantry, num_meals=self.num_meals,
                household_size=self.household_size, preferences=self.preferences,
                spec=spec, chat_fn=self.deps.planner_chat_fn)
        except Exception as exc:  # noqa: BLE001 - classified and recorded
            text = self._record_failure("chef", exc)
            replies = getattr(exc, "replies", [])
            self._log_call("chef", spec, replies, "", started, False, text)
            self.errors.append(f"dish planner: {text}")
            return False
        self._log_call("chef", spec, replies, prompt, started, True)
        self.deps.tracker.log_dict(dish_planner.plan_as_dict(self.plan), "dish_plan.json")
        if len(self.plan.dishes) < self.num_meals:
            self.warnings.append(f"The Chef planned {len(self.plan.dishes)} usable dish(es) "
                                 f"for {self.num_meals} meals")
        return True

    def _write_all(self) -> List[_DishOutcome]:
        assert self.plan is not None
        outcomes = [_DishOutcome(i, d) for i, d in enumerate(self.plan.dishes)]
        deal_rows = {int(d["deal_id"]): d for d in self.deals}
        kept_ids = self.selection.kept_ids()
        try:
            deal_products = self.deps.fetch_deal_products(kept_ids)
        except Exception as exc:  # noqa: BLE001 - degrade; never fail open
            logger.error("deal product links unavailable: %s", exc)
            deal_products = {}
            if products.restrictions_to_attributes(self.restrictions):
                self.warnings.append("Deal product links unavailable: dietary compliance "
                                     "cannot be confirmed for any deal")
        catalog = CitationCatalog(
            deal_products=deal_products,
            staples={str(p["product_id"]): p for p in self.staples})
        items = {t.deal_id: t.item for t in self.selection.proposal.by_id().values()}
        deal_rows = {i: {**row, "item": items.get(i)} for i, row in deal_rows.items()}
        context = dict(deals={i: deal_rows[i] for i in kept_ids if i in deal_rows},
                       deal_products=deal_products, catalog=catalog)

        for attempt in (1, 2):
            todo = [o for o in outcomes if not o.approved]
            if not todo or (attempt > 1 and self.infrastructure):
                break
            # Each worker runs in a copy of this context, so the MLflow
            # generation scope (a ContextVar) reaches the SousChef threads.
            jobs = [(contextvars.copy_context(), o) for o in todo]
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_DISHES, len(todo))) as pool:
                list(pool.map(lambda j: j[0].run(self._write_one, j[1], **context), jobs))
            self.progress("write_recipes")
        return outcomes

    def _write_one(self, o: _DishOutcome, *, deals, deal_products, catalog) -> None:
        feedback = None
        if o.attempts:
            feedback = ("; ".join(o.report["violations"]) if o.report and o.report["violations"]
                        else o.error)
        o.attempts += 1
        agent = f"sous_chef_{o.index + 1}"
        spec = self.deps.writer_spec
        started = time.monotonic()
        try:
            recipe, replies, prompt = dish_writer.write_recipe(
                o.dish, deals=deals, stock_up_ids=self.plan.stock_up_deal_ids,
                pantry=self.selection.pantry, servings=self.household_size,
                dietary_restrictions=self.restrictions, preferences=self.preferences,
                deal_products=deal_products, feedback=feedback, spec=spec,
                chat_fn=self.deps.writer_chat_fn, recipe_id=self.deps.recipe_ids, agent=agent)
        except Exception as exc:  # noqa: BLE001 - classified and recorded
            text = self._record_failure(agent, exc)
            self._log_call(agent, spec, getattr(exc, "replies", []), "", started, False, text,
                           order=(o.attempts, o.index))
            o.recipe, o.report, o.error = None, None, text
            return
        self._log_call(agent, spec, replies, prompt, started, True, order=(o.attempts, o.index))
        o.recipe = recipe
        o.report = compliance_report(recipe, self.restrictions, catalog)
        o.error = None if o.report["approved"] else "rejected: " + "; ".join(
            o.report["violations"])

    # -------------------------------------------------------------- endings

    def _generation_record(self, status: str, outcomes: Sequence[_DishOutcome],
                           num_saved: int, total_cost=None, savings=None,
                           failure_reason: Optional[str] = None) -> Dict[str, Any]:
        sel = self.selection
        return {
            "generation_id": self.generation_id, "status": status,
            "failure_reason": failure_reason, "num_meals_requested": self.num_meals,
            "num_recipes_saved": num_saved,
            "iteration_count": max((o.attempts for o in outcomes), default=0),
            "num_recipes_generated": sum(1 for o in outcomes if o.recipe is not None),
            "num_recipes_approved": sum(1 for o in outcomes if o.approved),
            "num_recipes_rejected": sum(1 for o in outcomes
                                        if o.recipe is not None and not o.approved),
            "dietary_restrictions": list(self.restrictions), "budget": None,
            "budget_period": None, "budget_spent_this_period": None,
            "household_size": self.household_size, "postal_code": self.postal_code,
            "preferences": self.preferences, "models": self.deps.models(),
            "snapshot": self.deps.snapshot,
            "mlflow_run_id": self.mlflow_run_id or None,
            "total_cost": total_cost, "estimated_savings": savings,
            "offered_deal_ids": sel.proposal.offered_ids(),
            "kept_deal_ids": sel.kept_ids(),
            "pantry_product_ids": [int(p["product_id"]) for p in sel.pantry],
            "dish_plan": dish_planner.plan_as_dict(self.plan) if self.plan else None,
        }

    def _base_result(self, outcomes: Sequence[_DishOutcome]) -> Dict[str, Any]:
        self.call_log.sort(key=lambda e: e["order"])
        generated = {o.recipe["recipe_id"]: o.recipe for o in outcomes if o.recipe}
        return {
            "generation_id": self.generation_id, "num_meals": self.num_meals,
            "generated_recipes": generated,
            "validation_results": {o.recipe["recipe_id"]: {"dietary_compliance": o.report,
                                                           "approved": o.approved}
                                   for o in outcomes if o.recipe},
            "dish_plan": dish_planner.plan_as_dict(self.plan) if self.plan else None,
            "agent_call_log": self.call_log, "infrastructure_errors": list(self.infrastructure),
            "budget_check": None,
            # For scorers: every deal the gate passed (kept_deal_ids is the
            # user's narrowing of the trays), what failed the verdict, passes.
            "available_deals": list(self.deals),
            "rejected_recipe_ids": [o.recipe["recipe_id"] for o in outcomes
                                    if o.recipe is not None and not o.approved],
            "iteration_count": result_iterations(outcomes),
            "kept_deal_ids": self.selection.kept_ids(),
        }

    def _fail(self, reason: str, outcomes: Sequence[_DishOutcome] = ()) -> Dict[str, Any]:
        logger.warning("Generation %s failed: %s", self.generation_id, reason)
        try:
            self.deps.recipe_store.save_recipes(
                self.user_id, [], generation=self._generation_record(
                    FAILED, outcomes, 0, failure_reason=reason))
        except Exception as exc:  # noqa: BLE001 - audit write must not mask the failure
            logger.error("Could not record failed generation %s: %s", self.generation_id, exc)
            self.warnings.append(f"The failed generation could not be recorded "
                                 f"({type(exc).__name__})")
        result = {**self._base_result(outcomes), "status": FAILED, "approved_recipe_ids": [],
                  "saved_recipe_ids": [], "total_cost": 0.0, "cost_per_meal": 0.0,
                  "estimated_savings": None, "warnings": self.warnings,
                  "errors": ["Unable to generate sufficient approved recipes", reason]}
        self.deps.tracker.log_final_metrics(total_cost=0.0, cost_per_meal=0.0,
                                            estimated_savings=None,
                                            iterations=result_iterations(outcomes),
                                            recipe_count=0, success=False)
        self.deps.tracker.finalize_run(result)
        self.progress("handle_failure")
        return result

    def _finalize(self, outcomes: Sequence[_DishOutcome]) -> Dict[str, Any]:
        approved = [o for o in outcomes if o.approved]
        recipes = [o.recipe for o in approved]
        pricing = price_plan(recipes, deals_by_id(self.deals))
        n = len(recipes)
        status = COMPLETED if n >= self.num_meals else "partial"
        if n < self.num_meals:
            self.warnings.append(f"Only {n}/{self.num_meals} recipes approved")
        persist = [{**o.recipe, "nutrition_facts": None, "health_score": None,
                    "dietary_compliance": o.report} for o in approved]
        saved = self.deps.recipe_store.save_recipes(
            self.user_id, persist, generation=self._generation_record(
                status, outcomes, n, pricing.total_cost, pricing.estimated_savings))
        result = {**self._base_result(outcomes), "status": COMPLETED,
                  "approved_recipe_ids": [r["recipe_id"] for r in recipes],
                  "saved_recipe_ids": saved, "total_cost": pricing.total_cost,
                  "cost_per_meal": pricing.total_cost / n if n else 0.0,
                  "estimated_savings": pricing.estimated_savings,
                  "warnings": self.warnings, "errors": []}
        self.deps.tracker.log_final_metrics(
            total_cost=pricing.total_cost, cost_per_meal=result["cost_per_meal"],
            estimated_savings=pricing.estimated_savings,
            iterations=result_iterations(outcomes), recipe_count=n, success=True)
        self.deps.tracker.finalize_run(result)
        self.progress("finalize_meal_plan")
        return result


def result_iterations(outcomes: Sequence[_DishOutcome]) -> int:
    return max((o.attempts for o in outcomes), default=0)
