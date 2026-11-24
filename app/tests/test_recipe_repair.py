"""The SousChef's safe repair and its retry feedback (``app/agents/recipe_repair.py``,
``dish_writer.accept_reply``). Deterministic: hand-made replies in the two
live failure shapes (``fixtures/writer_replies/``), no model, no database."""
import copy
import json
import random
from pathlib import Path

import pytest

from app.agents import recipe_repair
from app.agents.dish_writer import accept_reply, retry_feedback
from app.agents.instruction_refs import parse_step
from app.agents.llm_output import citation_context

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "writer_replies"
                      / "live-failure-shapes.json").read_text())
CONTEXT = citation_context(FIXTURE["offered"]["deal_ids"], FIXTURE["offered"]["product_ids"])


def case(name):
    c = FIXTURE["cases"][name]
    return copy.deepcopy(c["reply"]), c["anchor_deal_ids"]


def citations(ingredients):
    """The (kind, id) every ingredient cites, ignoring junk values."""
    out = []
    for ing in ingredients:
        get = ing.get if isinstance(ing, dict) else (lambda k, i=ing: getattr(i, k))
        for key in ("deal_id", "product_id"):
            v = get(key)
            if isinstance(v, str) and v.strip().isdigit():
                v = int(v)
            if isinstance(v, int) and not isinstance(v, bool):
                out.append((key, v))
    return out


def rejected(data, anchors):
    with pytest.raises(ValueError) as exc:
        accept_reply(data, CONTEXT, anchors)
    return str(exc.value)


# ------------------------------------------------------------ shape 1: "N/A"

def test_an_uncited_ingredient_is_dropped_and_the_placeholders_renumbered():
    data, anchors = case("uncited_na")
    model, repairs = accept_reply(data, CONTEXT, anchors)
    names = [i.name for i in model.ingredients]
    assert "mushrooms" not in names and len(names) == 10
    assert model.instructions == [
        "Cut {0} into cubes and marinate with {2}, {3}, {6}, {7}, and {8} for 10 minutes.",
        "Heat {1} in a wok or large pan over medium-high heat.",
        # "Add {2}, stir-fry ..." was about the mushrooms alone: dropped
        "Add {0} and {4}, {5}, and {8}, stir-fry for 3-4 minutes until well combined and "
        "heated through.",
        "Add {6} and {7}, toss to coat.",
        "Serve immediately."]
    # every placeholder still names the same ingredient it did before
    before = [i["name"] for i in data["ingredients"]]
    old_refs = [before[r] for s in data["instructions"] for r in parse_step(s).refs
                if before[r] != "mushrooms"]
    new_refs = [names[r] for s in model.instructions for r in parse_step(s).refs]
    assert new_refs == old_refs
    kinds = [r["kind"] for r in repairs]
    assert kinds == ["dropped_ingredient", "dropped_placeholder", "dropped_step",
                     "renumbered_placeholders"]
    assert repairs[0] == {"kind": "dropped_ingredient", "index": 2, "name": "mushrooms",
                          "deal_id": "N/A", "product_id": None}


@pytest.mark.parametrize("junk", [None, "", "  ", "N/A", "none", "null", "-1", "deal 8"])
def test_every_kind_of_missing_citation_counts_as_uncited(junk):
    data, anchors = case("uncited_na")
    data["ingredients"][2]["deal_id"] = junk
    model, repairs = accept_reply(data, CONTEXT, anchors)
    assert len(model.ingredients) == 10 and repairs


# ------------------------------------------------------------ shape 2: {12} of 11

def test_out_of_range_placeholders_are_removed_and_the_anchor_kept():
    data, anchors = case("out_of_range")
    model, repairs = accept_reply(data, CONTEXT, anchors)
    assert len(model.ingredients) == 10                  # the uncited chickpeas went
    assert ("deal_id", 9) in citations(model.ingredients)
    steps = model.instructions
    assert "Serve {11} over {12}." not in steps          # referred to nothing: dropped
    assert all(r < 10 for s in steps for r in parse_step(s).refs)
    reasons = {(r.get("index"), r.get("reason")) for r in repairs
               if r["kind"] == "dropped_placeholder"}
    assert (11, "out of range") in reasons and (0, "uncited ingredient") in reasons


def test_an_out_of_range_placeholder_in_a_list_is_removed_with_its_separator():
    data, anchors = case("uncited_na")
    data["instructions"][4] = "Add {7}, {8} and {11}, toss to coat."
    model, _ = accept_reply(data, CONTEXT, anchors)
    assert "Add {6}, {7}, toss to coat." in model.instructions


def test_the_live_curry_without_its_anchor_is_not_repaired():
    data, anchors = case("out_of_range_no_anchor")
    text = rejected(data, anchors)
    assert recipe_repair.repair_reply(data, anchors).refused == \
        "no remaining ingredient cites the dish's anchor deal"
    # ...and the model is told exactly what is wrong
    assert "step 7 uses {12}, but there are only 11 ingredients" in text
    assert "valid placeholders are {0}..{10}" in text


# ------------------------------------------------------------ never weaken

def test_a_valid_reply_is_accepted_untouched():
    data, anchors = case("uncited_na")
    del data["ingredients"][2]
    data["instructions"] = ["Fry {0} in {1}.", "Add {2}."]
    model, repairs = accept_reply(data, CONTEXT, anchors)
    assert repairs == [] and model.instructions == ["Fry {0} in {1}.", "Add {2}."]


@pytest.mark.parametrize("field,value", [("product_id", 50), ("product_id", "50")])
def test_junk_beside_a_real_citation_is_not_repaired(field, value):
    """Intent unclear: the model may have meant a deal it could not find.
    Not silently reinterpreted; it goes back to the model."""
    data, anchors = case("uncited_na")
    data["ingredients"][2][field] = value  # deal_id stays "N/A"
    text = rejected(data, anchors)
    assert "ingredient 2 'mushrooms'" in text


def test_an_id_that_was_not_offered_is_not_repaired():
    data, anchors = case("uncited_na")
    data["ingredients"][2]["deal_id"] = 999
    text = rejected(data, anchors)
    assert "cites deal_id 999, which is not listed above" in text


def test_a_repair_below_the_minimum_ingredient_count_is_refused():
    data = {"name": "Tofu", "ingredients": [
        {"name": "tofu", "deal_id": 27}, {"name": "oil", "product_id": 50},
        {"name": "x", "deal_id": "N/A"}],
        "instructions": ["Fry {0} in {1} with {2}."]}
    fix = recipe_repair.repair_reply(data, [27])
    assert fix.data is None and "minimum 3" in fix.refused
    rejected(data, [27])


def test_dropping_more_than_half_the_ingredients_is_refused():
    data = {"name": "Tofu", "ingredients": [
        {"name": "tofu", "deal_id": 27}, {"name": "oil", "product_id": 50},
        {"name": "salt", "product_id": 55}] + [{"name": f"x{i}", "deal_id": "N/A"}
                                              for i in range(4)],
        "instructions": ["Fry {0} in {1} with {2}, {3}, {4}, {5} and {6}."]}
    assert "4 of 7 ingredients" in recipe_repair.repair_reply(data, [27]).refused
    rejected(data, [27])


def test_dropping_more_than_half_the_steps_is_refused():
    data, anchors = case("uncited_na")
    data["instructions"] = ["Fry {0} in {1} with {3}, {4}, {5}, {6}, {7}, {8}, {9}, {10}.",
                            "Add {2}.", "Toss {2}.", "Serve {12}."]
    assert "3 of 4 steps" in recipe_repair.repair_reply(data, anchors).refused


def test_one_based_placeholders_are_not_silently_shifted():
    data, anchors = case("uncited_na")
    del data["ingredients"][2]  # 10 ingredients, all cited
    data["instructions"] = [f"Add {{{i}}}." for i in range(1, 11)]  # {1}..{10}, never {0}
    fix = recipe_repair.repair_reply(data, anchors)
    assert fix.data is None and "1-based" in fix.refused
    assert "uses {10}, but there are only 10 ingredients" in rejected(data, anchors)


def test_a_repaired_reply_must_still_pass_the_whole_schema():
    data, anchors = case("uncited_na")
    data["instructions"][1] = "Heat {1} in a {wok}."  # malformed brace: not repairable
    text = rejected(data, anchors)
    assert "malformed placeholder" in text


def test_repair_never_invents_or_changes_a_citation():
    """Randomised (seeded) replies: whatever is accepted cites a subset of the
    reply's own valid citations, in order, and no uncited ingredient survives."""
    rng = random.Random(1124)
    junk = [None, "", "N/A", "none"]
    base, anchors = case("uncited_na")
    accepted = 0
    for _ in range(400):
        data = copy.deepcopy(base)
        for ing in data["ingredients"][1:]:
            if rng.random() < 0.25:
                ing.pop("product_id", None)
                ing["deal_id"] = rng.choice(junk)
        n = len(data["ingredients"])
        data["instructions"] = [
            " and ".join("{%d}" % rng.randrange(n + 3) for _ in range(rng.randint(1, 3)))
            + rng.choice([" in a pan.", ", then stir."]) for _ in range(rng.randint(3, 7))]
        try:
            model, repairs = accept_reply(data, CONTEXT, anchors)
        except ValueError:
            continue
        accepted += 1
        before = citations(data["ingredients"])
        after = citations(model.ingredients)
        assert after == [c for c in before if c in after]  # an ordered subset
        assert len(after) == len(model.ingredients)        # every survivor cites
        assert all(r < len(model.ingredients) for s in model.instructions
                   for r in parse_step(s).refs)
        assert ("deal_id", 27) in after
    assert accepted > 50  # the property was exercised, not vacuous


# ------------------------------------------------------------ feedback

def test_feedback_lists_every_problem_at_once_without_pydantic_noise():
    """Pydantic stops at the ingredient errors and never reaches the steps,
    so the live model learned about {12} only after its retry was spent."""
    data, _ = case("out_of_range_no_anchor")
    data["ingredients"][0] = {"name": "chickpeas", "deal_id": "", "quantity": "1"}
    from app.agents.llm_output import RecipeBatch
    with pytest.raises(ValueError) as exc:
        RecipeBatch.model_validate(data, context=CONTEXT)
    assert "{12}" not in str(exc.value)  # the masking this fixes
    text = retry_feedback(data, exc.value, CONTEXT)
    assert "ingredient 0 'chickpeas' cites no listed id" in text and "REMOVE it" in text
    assert "step 7 uses {12}" in text
    assert "errors.pydantic.dev" not in text and "input_value" not in text


def test_feedback_falls_back_to_the_cleaned_schema_error():
    from app.agents.llm_output import RecipeBatch
    data = {"name": "x", "ingredients": [], "instructions": []}
    with pytest.raises(ValueError) as exc:
        RecipeBatch.model_validate(data, context=CONTEXT)
    text = retry_feedback(data, exc.value, CONTEXT)
    assert text and "errors.pydantic.dev" not in text and "[type=" not in text
