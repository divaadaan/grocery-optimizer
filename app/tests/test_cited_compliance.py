"""Recipe compliance and costing read ingredient citations, never names.

Pins the owner's decision: once a product is reviewed, its curated attributes
are the only authority on whether a recipe ingredient complies. Ingredients
cite a deal_id or a pantry-staple product_id; an uncited or un-offered
citation is an output-validation failure fed back to the model. No DB, no LLM.
"""
import json

import pytest

from app.agents import dietary
from app.agents.dietary import CitationCatalog, compliance_report
from app.services import mlflow_logger


def _product(pid, name, *, status="confirmed", **attrs):
    row = {
        "product_id": pid, "canonical_name": name, "status": status,
        "contains_meat": False, "contains_fish": False, "contains_dairy": False,
        "contains_egg": False, "contains_nuts": False,
        "contains_animal_byproduct": False,
        "contains_gluten": False,
    }
    row.update(attrs)
    return row


EGGPLANT = _product(40, "Eggplant")
COCONUT_MILK = _product(41, "Coconut Milk")
ALMOND_MILK = _product(42, "Almond Milk", contains_nuts=True)
CHICKEN = _product(43, "Chicken Breast", contains_meat=True)
PROSCIUTTO = _product(44, "Prosciutto", contains_meat=True)
PROPOSED_TOFU = _product(45, "Tofu", status="proposed")
CHEDDAR = _product(46, "Cheddar", contains_dairy=True)

SALT = _product(101, "Salt")
FISH_SAUCE = _product(102, "Fish Sauce", contains_fish=True)
BUTTER = _product(103, "Butter", contains_dairy=True)


def _deal(deal_id, name, sale, regular=None):
    return {"deal_id": deal_id, "product_name": name, "sale_price": sale,
            "regular_price": regular}


DEALS = [
    _deal(1, "EGGPLANT ITALIAN", 2.50),
    _deal(2, "AROY-D COCONUT MILK 400 ML", 1.99, 2.49),
    _deal(3, "SILK ALMOND BEVERAGE", 3.99),
    _deal(4, "Boneless Chicken", 8.99, 12.99),
    _deal(5, "PRIMO PROSCIUTTO", 4.99),
    _deal(6, "UNLINKED THING", 1.00),
    _deal(7, "TOFU", 2.49),
    _deal(8, "EGGPLANT OR CHEDDAR 2 FOR $5", 5.00),
]
CATALOG = CitationCatalog(
    deal_products={
        "1": [EGGPLANT], "2": [COCONUT_MILK], "3": [ALMOND_MILK], "4": [CHICKEN],
        "5": [PROSCIUTTO], "7": [PROPOSED_TOFU], "8": [EGGPLANT, CHEDDAR],
        # deal 6 has no junction row: unlinked
    },
    staples={"101": SALT, "102": FISH_SAUCE, "103": BUTTER},
)


def _recipe(*ingredients):
    return {"name": "Test Recipe", "ingredients": list(ingredients)}


def _rep(restrictions, *ingredients, catalog=CATALOG):
    return compliance_report(_recipe(*ingredients), restrictions, catalog)


@pytest.fixture(autouse=True)
def no_mlflow(monkeypatch):
    monkeypatch.setattr(mlflow_logger, "_ensure_configured", lambda: False)


# --------------------------------------------------------------------------
# The owner's acceptance cases
# --------------------------------------------------------------------------

def test_vegan_recipe_citing_eggplant_and_coconut_milk_is_approved():
    """The substring oracle rejected both: 'egg' in eggplant, 'milk' in
    coconut milk. Their curated attributes are all False."""
    report = compliance_report(
        _recipe({"name": "eggplant", "deal_id": 1}, {"name": "coconut milk", "deal_id": 2}),
        ["vegan"], CATALOG,
    )
    assert report["approved"] is True
    assert report["violations"] == []


def test_fish_sauce_staple_rejected_for_vegetarian_whatever_its_name():
    for display in ("Fish Sauce", "Vegetable seasoning", "salt"):
        report = compliance_report(
            _recipe({"name": display, "product_id": 102}), ["vegetarian"], CATALOG
        )
        assert report["approved"] is False, display
        assert report["violations"] == [
            f"{display} (pantry product 102): product 102 'Fish Sauce' has contains_fish"
        ]
        assert report["meets_restrictions"] is False


def test_display_name_is_never_read():
    """'beef' as a display name on a cited vegan product passes."""
    report = compliance_report(
        _recipe({"name": "beef", "deal_id": 1}, {"name": "bacon bits", "product_id": 101}),
        ["vegan", "no_nuts"], CATALOG,
    )
    assert report["approved"] is True


def test_cured_meat_without_a_forbidden_term_is_caught():
    """'prosciutto' contains no term in the old vocabulary; the attribute
    catches it regardless."""
    report = _rep(["vegetarian"], {"name": "prosciutto", "deal_id": 5})
    assert report["approved"] is False
    assert "contains_meat" in report["violations"][0]


def test_dietary_module_no_longer_matches_substrings_for_recipes():
    """recipe_violations takes a catalog; there is no name-based entry point,
    and the deprecated substring oracle is gone from the module entirely."""
    import inspect
    src = inspect.getsource(dietary)
    for name in ("forbidden_terms_for", "_name_violates", "is_compliant",
                 "MEAT_POULTRY", "FISH_SEAFOOD"):
        assert not hasattr(dietary, name), name
        assert name not in src, name


# --------------------------------------------------------------------------
# Fail-closed paths
# --------------------------------------------------------------------------

def test_unrestricted_user_is_trivially_approved_even_for_unresolvable_citations():
    report = compliance_report(
        _recipe({"name": "x", "deal_id": 6}, {"name": "y", "product_id": 999},
                {"name": "z"}),
        [], CATALOG,
    )
    assert report["approved"] is True


def test_unknown_restriction_label_is_not_enforced(caplog):
    report = _rep(["kosher"], {"name": "chicken", "deal_id": 4})
    assert report["approved"] is True


def test_deal_with_no_linked_product_fails_closed():
    report = _rep(["vegetarian"], {"name": "mystery", "deal_id": 6})
    assert report["approved"] is False
    assert "no confirmed product is linked" in report["violations"][0]


def test_deal_missing_from_the_catalog_entirely_fails_closed():
    report = _rep(["no_nuts"], {"name": "ghost", "deal_id": 999})
    assert report["approved"] is False
    assert report["allergen_free"] is False


def test_unconfirmed_linked_product_fails_closed():
    report = _rep(["vegan"], {"name": "tofu", "deal_id": 7})
    assert report["approved"] is False
    assert "not a confirmed product" in report["violations"][0]


def test_every_product_on_a_multi_product_tile_must_clear():
    report = _rep(["vegan"], {"name": "eggplant", "deal_id": 8})
    assert report["approved"] is False
    assert report["violations"] == [
        "eggplant (deal 8): product 46 'Cheddar' has contains_dairy"
    ]


def test_unknown_staple_fails_closed():
    report = _rep(["vegan"], {"name": "garlic", "product_id": 555})
    assert report["approved"] is False
    assert "not a known pantry staple" in report["violations"][0]


def test_uncited_ingredient_fails_closed_for_restricted_user():
    report = _rep(["vegan"], {"name": "salt"})
    assert report["approved"] is False
    assert "cites neither a deal nor a pantry product" in report["violations"][0]


def test_missing_attribute_counts_as_failing():
    partial = {"product_id": 9, "canonical_name": "Partial", "status": "confirmed"}
    catalog = CitationCatalog(deal_products={"9": [partial]})
    report = _rep(["pescatarian"], {"name": "p", "deal_id": 9}, catalog=catalog)
    assert report["approved"] is False
    assert "contains_meat" in report["violations"][0]


def test_allergen_and_diet_split():
    report = compliance_report(
        _recipe({"name": "almond milk", "deal_id": 3}), ["vegetarian", "no_nuts"], CATALOG
    )
    assert report["approved"] is False
    assert report["allergen_free"] is False
    assert report["meets_restrictions"] is True

    report = _rep(["vegan"], {"name": "butter", "product_id": 103})
    assert report["allergen_free"] is True
    assert report["meets_restrictions"] is False
    assert report["details"][0]["attributes"] == ["contains_dairy"]


def test_citation_catalog_from_state():
    state = {"pantry_staples": [SALT, FISH_SAUCE], "deal_products": {"1": [EGGPLANT]}}
    catalog = CitationCatalog.from_state(state)
    assert set(catalog.staples) == {"101", "102"}
    assert catalog.deal_products["1"] == [EGGPLANT]
    assert CitationCatalog.from_state({}).staples == {}


# --------------------------------------------------------------------------
# contains_animal_byproduct (migration 015): honey and undecided products
# --------------------------------------------------------------------------

HONEY = _product(104, "Honey", contains_animal_byproduct=True)
#: Confirmed before 015 and never revisited: the new column is NULL.
UNDECIDED_JAM = _product(47, "Strawberry Jam", contains_animal_byproduct=None)
BYPRODUCT_CATALOG = CitationCatalog(
    deal_products={"9": [UNDECIDED_JAM]},
    staples={"101": SALT, "104": HONEY},
)


def test_recipe_citing_honey_staple_is_rejected_for_a_vegan():
    report = _rep(["vegan"], {"name": "honey", "product_id": 104},
                  catalog=BYPRODUCT_CATALOG)
    assert report["approved"] is False
    assert report["meets_restrictions"] is False
    assert report["allergen_free"] is True
    assert report["details"][0]["attributes"] == ["contains_animal_byproduct"]
    assert report["violations"] == [
        "honey (pantry product 104): product 104 'Honey' has contains_animal_byproduct"
    ]


@pytest.mark.parametrize("restrictions", [["vegetarian"], ["pescatarian"],
                                          ["no_nuts"], ["dairy_free"]])
def test_recipe_citing_honey_staple_is_approved_for_non_vegans(restrictions):
    report = _rep(restrictions, {"name": "honey", "product_id": 104},
                  {"name": "salt", "product_id": 101}, catalog=BYPRODUCT_CATALOG)
    assert report["approved"] is True, report["violations"]


def test_undecided_byproduct_fails_closed_for_a_vegan_recipe():
    """NULL is not False: nobody has asked whether the jam contains, say,
    carmine. Rejected, and the feedback says it is undecided, not present."""
    report = _rep(["vegan"], {"name": "jam", "deal_id": 9}, catalog=BYPRODUCT_CATALOG)
    assert report["approved"] is False
    assert report["details"][0]["attributes"] == ["contains_animal_byproduct"]
    assert "undecided" in report["violations"][0]


def test_undecided_byproduct_does_not_affect_a_vegetarian_recipe():
    report = _rep(["vegetarian"], {"name": "jam", "deal_id": 9},
                  catalog=BYPRODUCT_CATALOG)
    assert report["approved"] is True


def test_honey_is_never_offered_to_a_vegan_sous_chef():
    from app.agents.sous_chef import offered_staples
    staples = [SALT, HONEY, {**UNDECIDED_JAM, "product_id": 105}]
    assert [s["product_id"] for s in offered_staples(staples, ["vegan"])] == [101]
    offered = offered_staples(staples, ["vegan", "no_nuts"])
    assert [s["product_id"] for s in offered] == [101]
    assert [s["product_id"] for s in offered_staples(staples, ["vegetarian"])] == [
        101, 104, 105]


def test_honey_staple_is_absent_from_a_vegan_sous_chef_prompt():
    chef = _sous_chef([_batch({"name": "Eggplant", "deal_id": 1},
                              {"name": "Salt", "product_id": 101})])
    chef.generate_recipes(
        chef_id="sous_chef_1", ingredient_group=[{"product_name": "E", "deal_id": 1}],
        target_recipe_count=1, household_size=2, dietary_restrictions=["vegan"],
        available_deals=DEALS, pantry_staples=[SALT, HONEY],
    )
    prompt = chef.llm.calls[0][0].content
    assert '"product_id": 101' in prompt
    assert '"product_id": 104' not in prompt and "Honey" not in prompt


def test_citing_an_unoffered_honey_staple_is_fed_back_to_a_vegan_sous_chef():
    chef = _sous_chef([
        _batch({"name": "Eggplant", "deal_id": 1}, {"name": "honey", "product_id": 104}),
        _batch({"name": "Eggplant", "deal_id": 1}),
    ])
    recipes = chef.generate_recipes(
        chef_id="sous_chef_1", ingredient_group=[{"product_name": "E", "deal_id": 1}],
        target_recipe_count=1, household_size=2, dietary_restrictions=["vegan"],
        available_deals=DEALS, pantry_staples=[SALT, HONEY],
    )
    assert "cites product_id 104" in chef.llm.calls[1][2].content
    assert [i.get("product_id") for i in recipes[0]["ingredients"]] == [None]


# --------------------------------------------------------------------------
# Nutritionist node
# --------------------------------------------------------------------------

class _AdvisoryLLM:
    def invoke(self, messages):
        class R:
            content = json.dumps({"approved": False, "health_score": 50})
        return R()


def _nutritionist():
    from app.agents.nutritionist import Nutritionist
    n = Nutritionist.__new__(Nutritionist)
    n.llm = _AdvisoryLLM()
    return n


def _nutritionist_state(recipes, restrictions):
    return {
        "generated_recipes": {r["recipe_id"]: r for r in recipes},
        "validation_results": {},
        "dietary_restrictions": restrictions,
        "approved_recipe_ids": [],
        "rejected_recipe_ids": [],
        "pantry_staples": [SALT, FISH_SAUCE, BUTTER],
        "deal_products": dict(CATALOG.deal_products),
    }


def test_nutritionist_verdict_ignores_the_advisory_llm_and_reads_citations():
    good = {"recipe_id": "a", **_recipe({"name": "eggplant", "deal_id": 1},
                                        {"name": "salt", "product_id": 101})}
    bad = {"recipe_id": "b", **_recipe({"name": "Vegetable stock", "product_id": 102})}
    state = _nutritionist_state([good, bad], ["vegetarian"])
    update = _nutritionist().validate_recipes(state)
    assert update["approved_recipe_ids"] == ["a"]
    assert update["rejected_recipe_ids"] == ["b"]
    feedback = update["validation_results"]["b"]["feedback"]
    assert "Vegetable stock" in feedback and "contains_fish" in feedback
    compliance = update["validation_results"]["b"]["dietary_compliance"]
    assert set(compliance) == {"allergen_free", "meets_restrictions", "violations"}


# --------------------------------------------------------------------------
# SousChef: what is offered, and citation validation in the retry loop
# --------------------------------------------------------------------------

class _ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)

        class R:
            content = self.responses.pop(0)
        return R()


def _sous_chef(responses):
    from app.agents.sous_chef import SousChef
    chef = SousChef.__new__(SousChef)
    chef.llm = _ScriptedLLM(responses)
    return chef


def test_offered_deals_resolve_by_id_then_exact_name_and_drop_the_rest():
    from app.agents.costing import deals_by_id
    from app.agents.sous_chef import offered_deals
    group = [
        {"product_name": "whatever the chef wrote", "deal_id": "1"},
        {"product_name": "Boneless Chicken", "deal_id": None},
        {"product_name": "Hallucinated", "deal_id": 999},
        {"product_name": "dup", "deal_id": 1},
    ]
    index = {d["product_name"]: d for d in DEALS}
    offered = offered_deals(group, deals_by_id(DEALS), index)
    assert [d["deal_id"] for d in offered] == [1, 4]
    assert offered[0]["product_name"] == "EGGPLANT ITALIAN"  # the deal's own name


def test_staples_offered_are_filtered_by_restriction():
    from app.agents.sous_chef import offered_staples
    staples = [SALT, FISH_SAUCE, BUTTER, _product(104, "Pending", status="proposed")]
    assert [s["product_id"] for s in offered_staples(staples, ["vegetarian"])] == [101, 103]
    assert [s["product_id"] for s in offered_staples(staples, ["vegan"])] == [101]
    assert [s["product_id"] for s in offered_staples(staples, [])] == [101, 102, 103]


def _batch(*ingredients):
    return json.dumps([{"name": "Ratatouille", "ingredients": list(ingredients),
                        "instructions": ["Cook."], "servings": 2}])


def test_generate_recipes_retries_on_unoffered_citation_and_prices_from_citations():
    chef = _sous_chef([
        _batch({"name": "chicken", "deal_id": 4}),                     # not offered
        _batch({"name": "fish sauce", "product_id": 102}),  # not offered to vegetarian
        _batch({"name": "Eggplant", "deal_id": 1, "price": 99},
               {"name": "Coconut milk", "deal_id": "2"},
               {"name": "Salt", "product_id": 101}),
    ])
    recipes = chef.generate_recipes(
        chef_id="sous_chef_1",
        ingredient_group=[{"product_name": "Eggplant", "deal_id": 1},
                          {"product_name": "Coconut", "deal_id": 2}],
        target_recipe_count=1, household_size=2, dietary_restrictions=["vegetarian"],
        available_deals=DEALS, pantry_staples=[SALT, FISH_SAUCE, BUTTER],
    )
    assert len(chef.llm.calls) == 3
    prompt = chef.llm.calls[0][0].content
    assert '"deal_id": 1' in prompt and '"product_id": 101' in prompt
    assert '"product_id": 102' not in prompt  # fish sauce never offered to a vegetarian
    assert "cites deal_id 4" in chef.llm.calls[1][2].content
    assert "cites product_id 102" in chef.llm.calls[2][4].content

    (recipe,) = recipes
    assert recipe["total_cost"] == round(2.50 + 1.99, 2)
    ings = recipe["ingredients"]
    assert [(i.get("deal_id"), i.get("product_id"), i["price"]) for i in ings] == [
        (1, None, 2.50), (2, None, 1.99), (None, 101, 0.0)
    ]
    assert "product_id" not in ings[0] and "deal_id" not in ings[2]


def test_generate_recipes_gives_up_after_repeated_uncited_output():
    chef = _sous_chef([_batch({"name": "Eggplant"})] * 3)
    recipes = chef.generate_recipes(
        chef_id="sous_chef_1", ingredient_group=[{"product_name": "E", "deal_id": 1}],
        target_recipe_count=1, household_size=2, dietary_restrictions=[],
        available_deals=DEALS, pantry_staples=[],
    )
    assert recipes == []
    assert len(chef.llm.calls) == 3


def test_generate_recipes_skips_a_group_with_nothing_citable():
    chef = _sous_chef([])
    recipes = chef.generate_recipes(
        chef_id="sous_chef_1", ingredient_group=[{"product_name": "Ghost", "deal_id": 999}],
        target_recipe_count=1, household_size=2, dietary_restrictions=[],
        available_deals=DEALS, pantry_staples=[SALT],
    )
    assert recipes == [] and chef.llm.calls == []


def test_regenerate_with_feedback_uses_the_same_citation_rules():
    chef = _sous_chef([
        json.dumps({"name": "Fixed", "ingredients": [{"name": "Eggplant", "deal_id": 1}],
                    "instructions": ["Cook."]}),
    ])
    recipe = chef.regenerate_with_feedback(
        chef_id="sous_chef_2", original_recipe={"name": "Old", "ingredients": []},
        feedback="Rejected: fish sauce has contains_fish",
        ingredient_group=[{"product_name": "Eggplant", "deal_id": 1}],
        household_size=2, dietary_restrictions=["vegetarian"],
        available_deals=DEALS, pantry_staples=[SALT, FISH_SAUCE],
    )
    assert recipe["total_cost"] == 2.50
    prompt = chef.llm.calls[0][0].content
    assert "contains_fish" in prompt and '"product_id": 101' in prompt


def test_sous_chef_prompts_take_the_pantry_list():
    from app.agents.prompts import PromptTemplates
    PromptTemplates.SOUS_CHEF_RECIPE_GENERATION.format(
        ingredients_json="[]", pantry_json="[]", target_recipe_count=1, servings=2,
        dietary_restrictions="",
    )
    PromptTemplates.SOUS_CHEF_RETRY_WITH_FEEDBACK.format(
        original_recipe_json="{}", feedback="", ingredients_json="[]", pantry_json="[]",
        servings=2,
    )


# --------------------------------------------------------------------------
# Graph: catalog loading and savings
# --------------------------------------------------------------------------

def test_load_recipe_catalog_reads_staples_and_links(monkeypatch):
    from app.agents import graph
    seen = {}
    monkeypatch.setattr(graph.recipe_catalog, "fetch_pantry_staples", lambda: [SALT])
    monkeypatch.setattr(graph.recipe_catalog, "fetch_deal_products",
                        lambda ids: seen.setdefault("ids", list(ids)) and {"1": [EGGPLANT]})
    update = graph.load_recipe_catalog(
        {"dietary_restrictions": ["vegan"], "available_deals": DEALS[:2]})
    assert update["pantry_staples"] == [SALT]
    assert update["deal_products"] == {"1": [EGGPLANT]}
    assert seen["ids"] == [1, 2]
    assert "warnings" not in update


def test_load_recipe_catalog_skips_links_for_unrestricted_users(monkeypatch):
    from app.agents import graph

    def boom(ids):
        raise AssertionError("should not be read")
    monkeypatch.setattr(graph.recipe_catalog, "fetch_pantry_staples", lambda: [])
    monkeypatch.setattr(graph.recipe_catalog, "fetch_deal_products", boom)
    update = graph.load_recipe_catalog(
        {"dietary_restrictions": [], "available_deals": DEALS})
    assert update["deal_products"] == {}


def test_load_recipe_catalog_degrades_without_failing_open(monkeypatch):
    from app.agents import graph

    def broken(*a):
        raise RuntimeError('column "assumed_on_hand" does not exist')
    monkeypatch.setattr(graph.recipe_catalog, "fetch_pantry_staples", broken)
    monkeypatch.setattr(graph.recipe_catalog, "fetch_deal_products", broken)
    update = graph.load_recipe_catalog(
        {"dietary_restrictions": ["vegan"], "available_deals": DEALS})
    assert update["pantry_staples"] == [] and update["deal_products"] == {}
    assert len(update["warnings"]) == 2


def test_graph_savings_come_from_cited_deals_and_never_invent_a_regular_price():
    from app.agents import graph
    state = {"available_deals": DEALS}
    no_regular = [_recipe({"name": "e", "deal_id": 1}, {"name": "s", "product_id": 101})]
    assert graph._savings(state, no_regular) is None
    with_regular = [_recipe({"name": "c", "deal_id": 2})]
    assert graph._savings(state, with_regular) == 0.5


# --------------------------------------------------------------------------
# recipe_catalog reads
# --------------------------------------------------------------------------

class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


class _DB:
    def __init__(self, cursor):
        self.cursor = cursor

    def get_cursor(self):
        db = self

        class Ctx:
            def __enter__(self_inner):
                return db.cursor

            def __exit__(self_inner, *exc):
                return False
        return Ctx()


def test_fetch_deal_products_groups_rows_by_deal():
    from app.services import recipe_catalog
    cursor = _Cursor([{"deal_id": 8, **EGGPLANT}, {"deal_id": 8, **CHEDDAR},
                      {"deal_id": 1, **EGGPLANT}])
    out = recipe_catalog.fetch_deal_products([8, "1", None, "x"], database=_DB(cursor))
    assert out == {"8": [EGGPLANT, CHEDDAR], "1": [EGGPLANT]}
    sql, params = cursor.executed[0]
    assert "deal_products" in sql and "p.status" in sql and "contains_nuts" in sql
    assert params == {"ids": [1, 8]}


def test_fetch_deal_products_with_no_ids_does_not_query():
    from app.services import recipe_catalog
    cursor = _Cursor([])
    assert recipe_catalog.fetch_deal_products([], database=_DB(cursor)) == {}
    assert cursor.executed == []


def test_fetch_pantry_staples_selects_confirmed_on_hand_products():
    from app.services import recipe_catalog
    cursor = _Cursor([SALT])
    assert recipe_catalog.fetch_pantry_staples(database=_DB(cursor)) == [SALT]
    sql = cursor.executed[0][0]
    assert "status = 'confirmed'" in sql and "assumed_on_hand" in sql


# --------------------------------------------------------------------------
# contains_gluten (migration 017)
# --------------------------------------------------------------------------

WHEAT_FLOUR = _product(110, "Wheat Flour", contains_gluten=True)
UNDECIDED_OATS = _product(111, "Rolled Oats", contains_gluten=None)
GLUTEN_CATALOG = CitationCatalog(
    deal_products={"9": [UNDECIDED_OATS]},
    staples={"101": SALT, "110": WHEAT_FLOUR},
)


def test_gluten_staple_is_rejected_for_a_gluten_free_recipe():
    report = _rep(["gluten_free"], {"name": "flour", "product_id": 110},
                  catalog=GLUTEN_CATALOG)
    assert report["approved"] is False
    assert report["details"][0]["attributes"] == ["contains_gluten"]


def test_undecided_gluten_fails_closed_for_a_gluten_free_recipe():
    report = _rep(["gluten_free"], {"name": "oats", "deal_id": 9},
                  catalog=GLUTEN_CATALOG)
    assert report["approved"] is False
    assert "undecided" in report["violations"][0]


@pytest.mark.parametrize("restrictions", [["vegan"], ["vegetarian"], ["no_nuts"]])
def test_gluten_does_not_affect_other_restrictions_in_a_recipe(restrictions):
    report = _rep(restrictions, {"name": "flour", "product_id": 110},
                  {"name": "oats", "deal_id": 9}, catalog=GLUTEN_CATALOG)
    assert report["approved"] is True, report["violations"]
