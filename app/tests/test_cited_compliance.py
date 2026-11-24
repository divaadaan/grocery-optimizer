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
        "not_kosher": False,
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


@pytest.mark.parametrize("labels", [["halal"], ["vegetarian", "halal"], ["Halal "]])
def test_unsupported_restriction_label_is_never_approved(labels):
    """A label no attribute enforces cannot be shown to be met: it is a
    violation of its own, never a vacuous "Approved: complies with halal"."""
    report = _rep(labels, {"name": "lentils", "product_id": 2})
    assert report["approved"] is False
    assert report["meets_restrictions"] is False
    assert any("'halal' is not a supported restriction" in v
               for v in report["violations"])


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
    from app.services.trays import offered_pantry
    staples = [SALT, HONEY, {**UNDECIDED_JAM, "product_id": 105}]
    assert [s["product_id"] for s in offered_pantry(staples, ["vegan"])] == [101]
    offered = offered_pantry(staples, ["vegan", "no_nuts"])
    assert [s["product_id"] for s in offered] == [101]
    assert [s["product_id"] for s in offered_pantry(staples, ["vegetarian"])] == [
        101, 104, 105]


def _curry_dish():
    from app.agents.dish_planner import PlannedDish
    return PlannedDish(name="Eggplant curry", anchor_deal_ids=[1])


def test_honey_staple_is_absent_from_a_vegan_sous_chef_prompt():
    from app.agents import dish_writer
    from app.services.trays import offered_pantry
    pantry = offered_pantry([SALT, HONEY], ["vegan"])
    prompt = dish_writer.build_prompt(_curry_dish(), [DEALS[0]], [], pantry, servings=2,
                                      dietary_restrictions=["vegan"])
    assert "→ product_id 101" in prompt
    assert "→ product_id 104" not in prompt and "honey" not in prompt.lower()


def test_citing_an_unoffered_honey_staple_is_fed_back_to_a_vegan_sous_chef():
    import json as _json
    from app.agents import dish_writer
    from app.agents.llm_chat import Reply
    from app.services.trays import offered_pantry

    def batch(*ingredients):
        return _json.dumps({"name": "Curry", "servings": 2, "ingredients": list(ingredients),
                            "instructions": [f"Cook {{{i}}}." for i in range(len(ingredients))]})

    replies = [batch({"name": "Eggplant", "deal_id": 1}, {"name": "honey", "product_id": 104}),
               batch({"name": "Eggplant", "deal_id": 1})]
    calls = []

    def chat_fn(spec, messages, **kw):
        calls.append(messages)
        return Reply(text=replies[len(calls) - 1])

    recipe, _, _ = dish_writer.write_recipe(
        _curry_dish(), deals={1: DEALS[0]}, stock_up_ids=[],
        pantry=offered_pantry([SALT, HONEY], ["vegan"]), servings=2,
        dietary_restrictions=["vegan"], spec="ollama:m", chat_fn=chat_fn)
    assert "cites product_id 104" in calls[1][2]["content"]
    assert [i.get("product_id") for i in recipe["ingredients"]] == [None]


# --------------------------------------------------------------------------
# recipe_catalog reads (what the pipeline's verdict catalog is built from)
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


# --------------------------------------------------------------------------
# kosher: not_kosher per product (migration 021) + "no meat with dairy"
# --------------------------------------------------------------------------

KOSHER_BEEF = _product(120, "Kosher Ground Beef", contains_meat=True)
KOSHER_CHEESE = _product(121, "Kosher Cheddar", contains_dairy=True)
KOSHER_SALMON = _product(122, "Salmon Fillet", contains_fish=True)
KOSHER_RICE = _product(123, "Rice")
UNCERTIFIED_CHEESE = _product(124, "Brie", contains_dairy=True, not_kosher=True)
UNDECIDED_PASTA = _product(125, "Pasta", not_kosher=None)
KOSHER_CATALOG = CitationCatalog(
    deal_products={
        "20": [KOSHER_BEEF], "21": [KOSHER_SALMON], "22": [UNCERTIFIED_CHEESE],
        "23": [UNDECIDED_PASTA], "24": [KOSHER_RICE],
        # One flyer tile linking both a meat and a dairy product.
        "25": [KOSHER_BEEF, KOSHER_CHEESE],
    },
    staples={"101": SALT, "121": KOSHER_CHEESE},
)


def _krep(restrictions, *ingredients):
    return compliance_report(_recipe(*ingredients), restrictions, KOSHER_CATALOG)


def test_kosher_requires_explicit_false_not_kosher():
    assert _krep(["kosher"], {"name": "rice", "deal_id": 24})["approved"] is True
    report = _krep(["kosher"], {"name": "brie", "deal_id": 22})
    assert report["approved"] is False
    assert report["details"][0]["attributes"] == ["not_kosher"]


def test_undecided_not_kosher_fails_closed():
    report = _krep(["kosher"], {"name": "pasta", "deal_id": 23})
    assert report["approved"] is False
    assert "not_kosher undecided" in report["violations"][0]


def test_meat_deal_with_dairy_staple_violates_kosher():
    report = _krep(["kosher"], {"name": "beef", "deal_id": 20},
                   {"name": "cheese", "product_id": 121})
    assert report["approved"] is False
    assert report["meets_restrictions"] is False
    [detail] = report["details"]
    assert detail["attributes"] == ["contains_meat", "contains_dairy"]
    assert detail["citation"] == "deal 20; pantry product 121"
    assert "kosher forbids combining contains_meat with contains_dairy" in detail["reason"]


def test_combination_violation_never_names_the_ingredient():
    report = _krep(["kosher"], {"name": "SECRET-INGREDIENT-NAME", "deal_id": 20},
                   {"name": "OTHER-SECRET", "product_id": 121})
    assert report["approved"] is False
    text = json.dumps(report)
    assert "SECRET-INGREDIENT-NAME" not in text and "OTHER-SECRET" not in text


def test_fish_with_dairy_is_kosher():
    report = _krep(["kosher"], {"name": "salmon", "deal_id": 21},
                   {"name": "cheese", "product_id": 121})
    assert report["approved"] is True, report["violations"]


def test_meat_with_pareve_is_kosher():
    report = _krep(["kosher"], {"name": "beef", "deal_id": 20},
                   {"name": "rice", "deal_id": 24}, {"name": "salt", "product_id": 101})
    assert report["approved"] is True, report["violations"]


def test_meat_with_fish_is_kosher():
    report = _krep(["kosher"], {"name": "beef", "deal_id": 20},
                   {"name": "salmon", "deal_id": 21})
    assert report["approved"] is True, report["violations"]


def test_a_tile_linking_meat_and_dairy_violates_kosher_when_cited_in_a_recipe():
    report = _krep(["kosher"], {"name": "tile", "deal_id": 25})
    assert report["approved"] is False
    assert report["details"][0]["citation"] == "deal 25"


@pytest.mark.parametrize("restrictions", [[], ["vegetarian"], ["no_nuts"], ["gluten_free"]])
def test_non_kosher_users_are_unaffected_by_the_combination_rule(restrictions):
    """No combination rule fires for anyone who is not kosher. (The full
    verdict is checked on meat + dairy except for vegetarian, where meat fails
    per product, so pareve rice + dairy stands in.)"""
    meat_and_dairy = _recipe({"name": "beef", "deal_id": 20},
                             {"name": "cheese", "product_id": 121})
    assert dietary.combination_violations(meat_and_dairy, restrictions, KOSHER_CATALOG) == []
    first = {"name": "m", "deal_id": 24 if restrictions == ["vegetarian"] else 20}
    report = _krep(restrictions, first, {"name": "cheese", "product_id": 121})
    assert report["approved"] is True, report["violations"]


def test_null_meat_or_dairy_fails_closed_in_the_combination_rule():
    odd_meat = _product(126, "Mystery", contains_meat=None)
    catalog = CitationCatalog(deal_products={"30": [odd_meat]},
                              staples={"121": KOSHER_CHEESE})
    report = compliance_report(
        _recipe({"name": "x", "deal_id": 30}, {"name": "y", "product_id": 121}),
        ["kosher"], catalog)
    assert report["approved"] is False
    assert "contains_meat undecided" in report["violations"][0]


def test_kosher_compliance_is_unchanged_when_every_name_is_nonsense():
    """Rename everything -- products and ingredients -- and every verdict,
    attribute and citation stands."""
    def scramble(p):
        return {**p, "canonical_name": "zzqx " + str(p["product_id"])}

    scrambled = CitationCatalog(
        deal_products={k: [scramble(p) for p in v]
                       for k, v in KOSHER_CATALOG.deal_products.items()},
        staples={k: scramble(p) for k, p in KOSHER_CATALOG.staples.items()},
    )
    cases = [
        [{"deal_id": 20}, {"product_id": 121}],
        [{"deal_id": 21}, {"product_id": 121}],
        [{"deal_id": 20}, {"deal_id": 24}],
        [{"deal_id": 22}],
        [{"deal_id": 23}],
        [{"deal_id": 25}],
    ]
    for case in cases:
        real = [{"name": "beef", **c} for c in case]
        nonsense = [{"name": f"qwerty{i}", **c} for i, c in enumerate(case)]
        a = compliance_report(_recipe(*real), ["kosher"], KOSHER_CATALOG)
        b = compliance_report(_recipe(*nonsense), ["kosher"], scrambled)
        assert a["approved"] == b["approved"], case
        assert [d["attributes"] for d in a["details"]] == \
               [d["attributes"] for d in b["details"]], case
        assert [d["citation"] for d in a["details"]] == \
               [d["citation"] for d in b["details"]], case
