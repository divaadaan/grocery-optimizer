"""Approach C generation (``app/agents/plan_pipeline.py``) with a scripted
model: dish planning, per-dish recipes, the dietary verdict, retries,
fail-fast and persistence. No database, no network, no live model."""
import json

import httpx
import pytest

from app.agents import dish_planner, dish_writer, plan_pipeline
from app.agents.llm_chat import ChatOutputError, Reply
from app.services import trays
from app.services.deal_labeller import Label

ATTRS = ("contains_meat", "contains_fish", "contains_dairy", "contains_egg", "contains_nuts",
         "contains_animal_byproduct", "not_kosher")


def product(pid, name, **attrs):
    row = {"product_id": pid, "canonical_name": name, "status": "confirmed",
           **{a: False for a in ATTRS}}
    row.update(attrs)
    return row


def deal(i, name, price, chain, unit=None):
    return {"deal_id": i, "product_name": name, "sale_price": price, "regular_price": None,
            "unit": unit, "category": None, "store_name": f"{chain} #1", "chain": chain,
            "store_id": 1, "source": "flipp", "product_id": None}


DEALS = [deal(1, "CHICKEN THIGHS", 1.99, "No Frills", "lb"),
         deal(2, "CHICKEN THIGHS", 3.49, "Metro", "lb"),
         deal(3, "FIRM TOFU 454G", 1.97, "T&T"),
         deal(4, "BROCCOLI CROWNS", 0.99, "Sobeys", "lb"),
         deal(5, "RUSSET POTATOES 10LB", 2.99, "Food Basics"),
         deal(6, "DRY PASTA 900G", 0.87, "Giant Tiger"),
         deal(7, "DRY PASTA 900G", 1.99, "Metro"),
         deal(8, "POTATO CHIPS", 1.50, "Metro")]
LABELS = {1: Label("chicken thighs", "protein"), 2: Label("chicken thighs", "protein"),
          3: Label("tofu", "protein", 454.0, "g"), 4: Label("broccoli", "vegetable"),
          5: Label("russet potatoes", "starch", 4536.0, "g"),
          6: Label("dry pasta", "pantry_staple", 900.0, "g"),
          7: Label("dry pasta", "pantry_staple", 900.0, "g"), 8: Label("chips", "snack")}
STAPLES = [product(50, "olive oil"), product(51, "salt"),
           product(52, "butter", contains_dairy=True, contains_animal_byproduct=True)]
DEAL_PRODUCTS = {"1": [product(100, "chicken thighs", contains_meat=True,
                               contains_animal_byproduct=True)],
                 "3": [product(101, "tofu")], "4": [product(102, "broccoli")],
                 "5": [product(103, "potatoes")], "6": [product(104, "pasta")]}


class Store:
    def __init__(self):
        self.saved = []

    def save_recipes(self, user_id, recipes, generation=None):
        self.saved.append((user_id, recipes, generation))
        return list(range(1, len(recipes) + 1))


class Tracker:
    def __init__(self):
        self.calls, self.dicts, self.final = [], {}, None

    def start_run(self, **kw):
        return "run-1"

    def log_agent_call(self, **kw):
        self.calls.append(kw)

    def log_dict(self, data, name):
        self.dicts[name] = data

    def log_final_metrics(self, **kw):
        self.final = kw

    def finalize_run(self, state):
        pass


def recipe_json(name, deal_ids=(), product_ids=()):
    ings = [{"name": f"d{d}", "deal_id": d, "quantity": "1", "unit": "lb"} for d in deal_ids]
    ings += [{"name": f"p{p}", "product_id": p, "quantity": "1", "unit": "tbsp"}
             for p in product_ids]
    steps = [f"Cook {{{i}}}." for i in range(len(ings))]
    return json.dumps({"name": name, "ingredients": ings, "instructions": steps,
                       "servings": 2, "estimated_prep_time": 30})


class Model:
    """Scripted chat_fn: planner replies first, then writer replies by dish name."""

    def __init__(self, plan, recipes, plan_replies=None):
        self.plan_replies = list(plan_replies or [json.dumps(plan)])
        self.recipes = recipes  # dish name -> list of reply texts (consumed in order)
        self.prompts = []

    def __call__(self, spec, messages, **kw):
        prompt = messages[0]["content"]
        self.prompts.append((spec, messages))
        if "home cook planning meals" in prompt:
            return Reply(text=self.plan_replies.pop(0), input_tokens=100, output_tokens=50)
        for name, replies in self.recipes.items():
            if f"**The dish:** {name}\n" in prompt:
                text = replies.pop(0) if len(replies) > 1 else replies[0]
                if isinstance(text, Exception):
                    raise text
                return Reply(text=text, input_tokens=80, output_tokens=40)
        raise AssertionError("unexpected prompt")


PLAN = {"dishes": [
    {"name": "Chicken with potatoes", "anchor_deal_ids": [1], "side_deal_ids": [5],
     "pantry_product_ids": [50], "why": "cheap thighs"},
    {"name": "Tofu broccoli stir-fry", "anchor_deal_ids": [3], "side_deal_ids": [4],
     "pantry_product_ids": [51], "why": "veg night"}],
    "stock_up_deal_ids": [6], "notes": "ok"}


def run(model, restrictions=(), kept=None, pantry=None, num_meals=2, store=None,
        tracker=None, fetch_deal_products=lambda ids: DEAL_PRODUCTS):
    deps = plan_pipeline.PlanDeps(
        fetch_deals=lambda pc, r: [dict(d) for d in DEALS],
        fetch_labels=lambda ids: {i: LABELS[i] for i in ids if i in LABELS},
        fetch_pantry=lambda: [dict(s) for s in STAPLES],
        fetch_deal_products=fetch_deal_products,
        recipe_store=store or Store(), tracker=tracker or Tracker(), chat_fn=model,
        planner_spec="ollama:planner", writer_spec="hf:org/writer")
    return plan_pipeline.run_plan_generation(
        user_id=7, postal_code="L4G3E8", household_size=2,
        dietary_restrictions=list(restrictions), num_meals=num_meals, preferences={},
        kept_deal_ids=kept, pantry_product_ids=pantry, deps=deps, generation_id="gen-1")


# ------------------------------------------------------------ selection

def proposal():
    p = trays.build_trays(DEALS, LABELS)
    p.pantry = trays.offered_pantry(STAPLES, [])
    return p


def test_selection_keeps_only_what_is_offered_and_reports_the_rest():
    sel = plan_pipeline.confirm_selection(proposal(), [1, 4, 999, 8], [50, 77])
    assert sel.kept_ids() == [1, 4]           # 8 (snack) was never offered
    assert sel.dropped_deal_ids == [8, 999]
    assert [p["product_id"] for p in sel.pantry] == [50] and sel.dropped_pantry_ids == [77]


def test_selection_defaults_to_everything_offered():
    sel = plan_pipeline.confirm_selection(proposal(), None, None)
    assert set(sel.kept_ids()) == set(proposal().offered_ids())
    assert len(sel.pantry) == 3


def test_selection_without_an_anchor_is_refused():
    with pytest.raises(plan_pipeline.SelectionError, match="anchor"):
        plan_pipeline.confirm_selection(proposal(), [4, 5], None)


# ------------------------------------------------------------ planner contract

def _ctx():
    sel = plan_pipeline.confirm_selection(proposal(), None, None)
    return dish_planner.plan_context(sel.kept, sel.pantry, 2), sel


def test_plan_validation_accepts_a_good_plan_and_anchor_reuse():
    ctx, _ = _ctx()
    plan = {**PLAN, "dishes": [PLAN["dishes"][0], {**PLAN["dishes"][0], "name": "Chicken soup"}]}
    assert len(dish_planner.DishPlan.model_validate(plan, context=ctx).dishes) == 2


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["dishes"].append(dict(p["dishes"][0])), "at most 2 dishes"),
    (lambda p: p["dishes"][0].update(anchor_deal_ids=[4]), "are not ANCHORS"),
    (lambda p: p["dishes"][0].update(side_deal_ids=[8]), "were not offered"),
    (lambda p: p["dishes"][0].update(pantry_product_ids=[999]), "not in the pantry"),
    (lambda p: p.update(stock_up_deal_ids=[4]), "not STOCK-UP"),
    (lambda p: p["dishes"][0].update(anchor_deal_ids=[]), "at least 1"),
])
def test_plan_validation_rejects(mutate, needle):
    ctx, _ = _ctx()
    plan = json.loads(json.dumps(PLAN))
    mutate(plan)
    with pytest.raises(ValueError, match=needle):
        dish_planner.DishPlan.model_validate(plan, context=ctx)


def test_planner_prompt_shows_trays_pantry_and_strict_contract():
    _, sel = _ctx()
    prompt = dish_planner.build_prompt(sel.kept, sel.pantry, 2, 2)
    assert "ANCHORS" in prompt and "1 | CHICKEN THIGHS | $1.99/lb" in prompt
    assert "50 | olive oil" in prompt
    assert "//" not in prompt and "exactly 2 dishes" in prompt
    assert "POTATO CHIPS" not in prompt


# ------------------------------------------------------------ end to end

def test_happy_path_plans_writes_approves_and_persists():
    model = Model(PLAN, {"Chicken with potatoes": [recipe_json("Chicken", [1, 5], [50])],
                         "Tofu broccoli stir-fry": [recipe_json("Tofu", [3, 4], [51])]})
    store, tracker = Store(), Tracker()
    result = run(model, store=store, tracker=tracker)
    assert result["status"] == "completed"
    assert len(result["approved_recipe_ids"]) == 2 and result["saved_recipe_ids"] == [1, 2]
    names = [result["generated_recipes"][r]["name"] for r in result["approved_recipe_ids"]]
    assert names == ["Chicken", "Tofu"]
    # costed from citations, each deal once: 1.99 + 2.99 + 1.97 + 0.99
    assert result["total_cost"] == pytest.approx(7.94)
    assert result["estimated_savings"] is None
    _, recipes, gen = store.saved[0]
    assert gen["status"] == "completed" and gen["budget"] is None
    assert set(gen["offered_deal_ids"]) == set(gen["kept_deal_ids"])
    assert gen["pantry_product_ids"] == [50, 51, 52]
    assert gen["dish_plan"]["dishes"][0]["anchor_deal_ids"] == [1]
    assert gen["models"] == {"chef": {"provider": "ollama", "model": "planner"},
                             "sous_chef": {"provider": "hf", "model": "org/writer"}}
    assert all(r["dietary_compliance"]["approved"] for r in recipes)
    assert recipes[0]["nutrition_facts"] is None
    assert "dish_plan.json" in tracker.dicts
    assert {c["agent_name"] for c in tracker.calls} == {"chef", "sous_chef_1", "sous_chef_2"}


def test_writer_sees_only_its_dish_stock_up_and_pantry():
    model = Model(PLAN, {"Chicken with potatoes": [recipe_json("Chicken", [1], [50])],
                         "Tofu broccoli stir-fry": [recipe_json("Tofu", [3], [51])]})
    run(model)
    chicken = next(m[0]["content"] for s, m in model.prompts if "**The dish:** Chicken" in
                   m[0]["content"])
    assert "- chicken thighs → deal_id 1 (" in chicken
    assert "- russet potatoes → deal_id 5 (" in chicken
    assert "→ deal_id 6 (" in chicken           # stock-up
    assert "→ deal_id 3 (" not in chicken and "→ deal_id 4 (" not in chicken
    # pantry: name first, alphabetical
    assert chicken.index("- butter → product_id 52") < chicken.index("- olive oil → product_id 50")
    assert "Use ONLY the assigned deals" not in chicken


def test_a_citation_outside_the_dish_is_retried_then_the_dish_dropped():
    bad = recipe_json("Chicken", [1, 3], [50])  # 3 is the tofu dish's anchor
    model = Model(PLAN, {"Chicken with potatoes": [bad],
                         "Tofu broccoli stir-fry": [recipe_json("Tofu", [3, 4], [51])]})
    result = run(model, num_meals=2)
    # 1/2 approved = 50% < 60% -> failed
    assert result["status"] == "failed"
    assert any("Insufficient" in e for e in result["errors"])


def test_dietary_rejection_gets_one_retry_with_the_reason():
    # Defence in depth: here the (fake) gate lets the chicken through for a
    # vegan user, so the recipe verdict -- curated attributes of the cited
    # products -- is what catches it, and the retry is told why.
    plan = {"dishes": [{"name": "Tofu bowl", "anchor_deal_ids": [3], "side_deal_ids": [1, 4],
                        "pantry_product_ids": [50]}], "stock_up_deal_ids": []}
    model = Model(plan, {"Tofu bowl": [recipe_json("Tofu", [3, 1], [50]),
                                       recipe_json("Tofu", [3, 4], [50])]})
    result = run(model, restrictions=["vegan"], num_meals=1)
    assert result["status"] == "completed"
    writer = [m[0]["content"] for s, m in model.prompts if "**The dish:** Tofu bowl" in m[0]["content"]]
    assert len(writer) == 2
    assert "was rejected" not in writer[0]
    assert "was rejected" in writer[1] and "chicken thighs" in writer[1]
    recipe = result["generated_recipes"][result["approved_recipe_ids"][0]]
    assert {i.get("deal_id") for i in recipe["ingredients"]} == {3, 4, None}


def test_vegan_never_sees_the_chicken_because_the_gate_ran_first():
    # The pipeline passes restrictions to fetch_deals: the gate's job. Here
    # the fake gate drops deal 1/2 for a vegan user; the trays never offer it.
    seen = {}

    def fetch_deals(pc, r):
        seen["r"] = r
        return [dict(d) for d in DEALS if not (r and d["deal_id"] in (1, 2))]

    plan = {"dishes": [{"name": "Tofu bowl", "anchor_deal_ids": [3], "side_deal_ids": [4]}],
            "stock_up_deal_ids": []}
    model = Model(plan, {"Tofu bowl": [recipe_json("Tofu", [3, 4], [50])]})
    deps = plan_pipeline.PlanDeps(
        fetch_deals=fetch_deals, fetch_labels=lambda ids: {i: LABELS[i] for i in ids},
        fetch_pantry=lambda: STAPLES, fetch_deal_products=lambda ids: DEAL_PRODUCTS,
        recipe_store=Store(), tracker=Tracker(), chat_fn=model,
        planner_spec="ollama:p", writer_spec="ollama:w")
    result = plan_pipeline.run_plan_generation(
        user_id=1, postal_code="X", household_size=1, dietary_restrictions=["vegan"],
        num_meals=1, deps=deps)
    assert seen["r"] == ["vegan"] and result["status"] == "completed"
    planner_prompt = model.prompts[0][1][0]["content"]
    assert "CHICKEN" not in planner_prompt


def test_malformed_plan_is_retried_once_then_the_run_fails():
    model = Model(PLAN, {}, plan_replies=["{not json", '{"dishes": []}'])
    store = Store()
    result = run(model, store=store)
    assert result["status"] == "failed"
    assert any("dish planner" in e for e in result["errors"])
    assert len(model.prompts) == 2
    assert store.saved[0][2]["status"] == "failed" and store.saved[0][1] == []


def test_partial_plan_at_sixty_percent():
    plan = {"dishes": [{"name": f"Dish {i}", "anchor_deal_ids": [1]} for i in range(5)],
            "stock_up_deal_ids": []}
    good = recipe_json("Chicken", [1], [50])
    recipes = {f"Dish {i}": [good] for i in range(3)}
    recipes.update({f"Dish {i}": ["nope"] for i in range(3, 5)})
    result = run(Model(plan, recipes), num_meals=5)
    assert result["status"] == "completed"   # job status; the row says partial
    assert len(result["approved_recipe_ids"]) == 3
    assert any("Only 3/5" in w for w in result["warnings"])


def test_infrastructure_failure_fails_fast_without_retries():
    req = httpx.Request("POST", "http://ollama")
    down = httpx.ConnectError("refused", request=req)
    model = Model(PLAN, {"Chicken with potatoes": [down, down],
                         "Tofu broccoli stir-fry": [down, down]})
    result = run(model)
    assert result["status"] == "failed"
    writer_calls = [p for p in model.prompts if "**The dish:**" in p[1][0]["content"]]
    assert len(writer_calls) == 2           # one attempt per dish, no retry round
    assert result["infrastructure_errors"]
    assert any("Infrastructure failure" in e for e in result["errors"])


def test_unsupported_restriction_refuses_before_any_work():
    from app.services.products import UnsupportedRestrictionError

    with pytest.raises(UnsupportedRestrictionError):
        run(Model(PLAN, {}), restrictions=["halal"])


def test_restricted_user_fails_closed_when_deal_links_are_unavailable():
    def boom(ids):
        raise RuntimeError("db down")

    plan = {"dishes": [{"name": "Tofu bowl", "anchor_deal_ids": [3]}], "stock_up_deal_ids": []}
    model = Model(plan, {"Tofu bowl": [recipe_json("Tofu", [3], [50])]})
    result = run(model, restrictions=["vegan"], num_meals=1, fetch_deal_products=boom)
    assert result["status"] == "failed"
    assert any("cannot be confirmed" in w for w in result["warnings"])


def test_ingredient_names_come_from_the_citation_not_the_model():
    text = json.dumps({"name": "Chicken", "ingredients": [
        {"name": "CHICKEN THIGHS FLYER TEXT", "deal_id": 1, "quantity": "1", "unit": "lb"},
        {"name": "ground cinnamon", "product_id": 51, "quantity": "1", "unit": "tsp"}],
        "instructions": ["Season {0} with {1}."], "servings": 2})
    plan = {"dishes": [{"name": "Chicken", "anchor_deal_ids": [1]}], "stock_up_deal_ids": []}
    result = run(Model(plan, {"Chicken": [text]}), num_meals=1)
    ings = result["generated_recipes"][result["approved_recipe_ids"][0]]["ingredients"]
    assert [i["name"] for i in ings] == ["chicken thighs", "salt"]
    assert ings[1]["written_as"] == "ground cinnamon"


def test_steps_render_deals_by_label_item():
    text = json.dumps({"name": "Chicken", "ingredients": [
        {"name": "x", "deal_id": 2, "quantity": "1", "unit": "lb"}],
        "instructions": ["Brown {0}."], "servings": 2})
    plan = {"dishes": [{"name": "Chicken", "anchor_deal_ids": [1], "side_deal_ids": []}],
            "stock_up_deal_ids": []}
    # deal 2 (Metro thighs) is not a tray item (deal 1 is cheaper), so cite 1
    text = text.replace('"deal_id": 2', '"deal_id": 1')
    result = run(Model(plan, {"Chicken": [text]}), num_meals=1)
    recipe = result["generated_recipes"][result["approved_recipe_ids"][0]]
    # deal 1 links one confirmed product, so its canonical name wins
    assert recipe["instructions"] == ["Brown chicken thighs."]
    assert recipe["ingredients"][0]["price"] == 1.99


# ------------------------------------------------------------ robustness

def _happy_model():
    return Model(PLAN, {"Chicken with potatoes": [recipe_json("Chicken", [1, 5], [50])],
                        "Tofu broccoli stir-fry": [recipe_json("Tofu", [3, 4], [51])]})


def test_a_failing_progress_callback_never_fails_the_generation():
    deps = plan_pipeline.PlanDeps(
        fetch_deals=lambda pc, r: [dict(d) for d in DEALS],
        fetch_labels=lambda ids: {i: LABELS[i] for i in ids if i in LABELS},
        fetch_pantry=lambda: [dict(s) for s in STAPLES],
        fetch_deal_products=lambda ids: DEAL_PRODUCTS, recipe_store=Store(),
        tracker=Tracker(), chat_fn=_happy_model(), planner_spec="ollama:p",
        writer_spec="ollama:w")

    def angry(stage):
        raise RuntimeError("progress sink exploded")

    result = plan_pipeline.run_plan_generation(
        user_id=7, postal_code="L4G3E8", household_size=2, dietary_restrictions=[],
        num_meals=2, deps=deps, progress_callback=angry)
    assert result["status"] == "completed"


class BrokenStore:
    def save_recipes(self, *a, **k):
        raise RuntimeError("neon is down")


def test_a_failed_audit_write_does_not_mask_the_failure():
    model = Model(PLAN, {}, plan_replies=["{nope", "{nope"])
    result = run(model, store=BrokenStore())
    assert result["status"] == "failed"
    assert any("could not be recorded" in w for w in result["warnings"])


def test_a_failed_save_in_the_success_path_still_raises():
    with pytest.raises(RuntimeError, match="neon is down"):
        run(_happy_model(), store=BrokenStore())


def test_one_generation_id_per_run_reaches_the_store():
    store = Store()
    result = plan_pipeline.run_plan_generation(
        user_id=7, postal_code="L4G3E8", household_size=2, dietary_restrictions=[],
        num_meals=2, deps=plan_pipeline.PlanDeps(
            fetch_deals=lambda pc, r: [dict(d) for d in DEALS],
            fetch_labels=lambda ids: {i: LABELS[i] for i in ids if i in LABELS},
            fetch_pantry=lambda: [dict(s) for s in STAPLES],
            fetch_deal_products=lambda ids: DEAL_PRODUCTS, recipe_store=store,
            tracker=Tracker(), chat_fn=_happy_model(), planner_spec="ollama:p",
            writer_spec="ollama:w"))
    import uuid as _uuid
    _uuid.UUID(result["generation_id"])
    assert store.saved[0][2]["generation_id"] == result["generation_id"]


def test_the_call_log_is_ordered_by_round_and_dish_not_completion():
    result = run(_happy_model())
    assert [e["agent"] for e in result["agent_call_log"]] == ["chef", "sous_chef_1", "sous_chef_2"]


# ------------------------------------------------------------ live (-m llm)

@pytest.mark.llm
def test_live_pipeline_on_a_frozen_snapshot():
    """End to end against the configured live models (LLM_PLANNER_MODEL /
    LLM_WRITER_MODEL) on the committed week-2026-10-16 snapshot. Nothing is
    written: the snapshot's PlanDeps use an in-memory store. Deselected from
    the oracle; run with ``pytest -m llm``."""
    from app.config import settings
    from app.eval.runner import NullTracker
    from app.eval.snapshots import load_snapshot

    snapshot = load_snapshot("week-2026-10-16")
    deps = snapshot.plan_deps(tracker=NullTracker(), planner_spec=settings.llm_planner_model,
                              writer_spec=settings.llm_writer_model)
    result = plan_pipeline.run_plan_generation(
        user_id=1, postal_code="M5V3A8", household_size=2,
        dietary_restrictions=["vegetarian"], num_meals=3, deps=deps)
    assert result["status"] == "completed", result["errors"]
    assert len(result["approved_recipe_ids"]) >= 2
    gated = {d["deal_id"] for d in result["available_deals"]}
    for rid in result["approved_recipe_ids"]:
        recipe = result["generated_recipes"][rid]
        assert result["validation_results"][rid]["dietary_compliance"]["approved"]
        assert all(i.get("deal_id") in gated or i.get("product_id") is not None
                   for i in recipe["ingredients"])


def test_stray_optional_ids_are_dropped_and_recorded_not_fatal():
    ctx, _ = _ctx()
    plan = json.loads(json.dumps(PLAN))
    plan["dishes"][0]["side_deal_ids"] = [5, 8, 999]          # 8: chips (never offered)
    plan["dishes"][1]["pantry_product_ids"] = [51, 77]
    plan["stock_up_deal_ids"] = [6, 4]                       # 4 is a side, not stock-up
    fixed = dish_planner.DishPlan.model_validate(dish_planner.repair_plan(plan, ctx),
                                                 context=ctx)
    assert fixed.dishes[0].side_deal_ids == [5]
    assert fixed.dishes[1].pantry_product_ids == [51]
    assert fixed.stock_up_deal_ids == [6]
    assert {(d["where"], d["id"]) for d in fixed.dropped} == {
        ("dish 1 side_deal_ids", 8), ("dish 1 side_deal_ids", 999),
        ("dish 2 pantry_product_ids", 77), ("stock_up_deal_ids", 4)}


def test_a_dish_without_a_valid_anchor_is_dropped_never_given_one():
    ctx, _ = _ctx()
    plan = json.loads(json.dumps(PLAN))
    plan["dishes"][0]["anchor_deal_ids"] = [4]                # broccoli: a side
    fixed = dish_planner.DishPlan.model_validate(dish_planner.repair_plan(plan, ctx),
                                                 context=ctx)
    assert [d.name for d in fixed.dishes] == ["Tofu broccoli stir-fry"]
    assert fixed.dropped[0]["where"] == "dish 1" and fixed.dropped[0]["reason"].startswith("no anchor")


def test_a_side_cited_beside_a_real_anchor_moves_to_the_sides():
    ctx, _ = _ctx()
    plan = json.loads(json.dumps(PLAN))
    plan["dishes"][1]["anchor_deal_ids"] = [3, 4]
    fixed = dish_planner.DishPlan.model_validate(dish_planner.repair_plan(plan, ctx),
                                                 context=ctx)
    assert fixed.dishes[1].anchor_deal_ids == [3] and fixed.dishes[1].side_deal_ids == [4]


def test_extra_dishes_are_dropped_and_no_usable_dish_is_retried():
    ctx, _ = _ctx()
    plan = json.loads(json.dumps(PLAN))
    plan["dishes"].append(dict(plan["dishes"][0], name="Third"))
    fixed = dish_planner.DishPlan.model_validate(dish_planner.repair_plan(plan, ctx),
                                                 context=ctx)
    assert len(fixed.dishes) == 2 and fixed.dropped[-1]["reason"] == "more dishes than meals"
    bad = {"dishes": [dict(PLAN["dishes"][0], anchor_deal_ids=[4])]}
    with pytest.raises(ValueError):
        dish_planner.DishPlan.model_validate(dish_planner.repair_plan(bad, ctx), context=ctx)


def test_a_plan_short_of_dishes_runs_and_says_so():
    plan = json.loads(json.dumps(PLAN))
    plan["dishes"][1]["anchor_deal_ids"] = [4]
    model = Model(plan, {"Chicken with potatoes": [recipe_json("Chicken", [1, 5], [50])]})
    result = run(model)
    assert any("planned 1 usable dish" in w for w in result["warnings"])
    assert result["status"] == "failed"   # 1 approved of 2 meals < 60%
    assert len(result["generated_recipes"]) == 1
    # With 3 meals a short plan of 2 good dishes is a partial (2/3 >= 60%).
    plan3 = json.loads(json.dumps(PLAN))
    plan3["dishes"].append(dict(PLAN["dishes"][0], name="Anchorless", anchor_deal_ids=[4]))
    model3 = Model(plan3, {"Chicken with potatoes": [recipe_json("C", [1, 5], [50])],
                           "Tofu broccoli stir-fry": [recipe_json("T", [3, 4], [51])]})
    partial = run(model3, num_meals=3)
    assert partial["status"] == "completed" and len(partial["approved_recipe_ids"]) == 2
    assert any("Only 2/3" in w for w in partial["warnings"])


def test_a_plan_with_a_stray_side_still_runs():
    plan = json.loads(json.dumps(PLAN))
    plan["dishes"][0]["side_deal_ids"] = [5, 999]
    model = Model(plan, {"Chicken with potatoes": [recipe_json("Chicken", [1, 5], [50])],
                         "Tofu broccoli stir-fry": [recipe_json("Tofu", [3, 4], [51])]})
    result = run(model)
    assert result["status"] == "completed"
    assert result["dish_plan"]["dropped"] == [{"where": "dish 1 side_deal_ids", "id": 999}]
    assert len(model.prompts) == 3                           # no planner retry
