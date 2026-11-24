#!/usr/bin/env python
"""The 2026-11-24 labelling pass: live deal names that feed the trays.

I am the labeller (``scripts/propose_products.py`` has no API client by
design). Entries are keyed by INDEX into ``names.json`` -- the frozen list of
unmapped, currently valid deal names whose stored ``deal_labels`` role is a
tray role -- never by a retyped string, because ``--import`` matches
``raw_name`` exactly.

The deal_labels role/item only decided WHICH names were worked on and in what
order (surfacing). Every dietary attribute below is my judgement of the
product as sold in Canadian flyers, applying the owner's labelling rules:
soft bread is not dairy/egg; traditional-rennet cheeses (parmesan, grana,
pecorino/romano, gruyere, asiago) are meat and other cheeses are not; honey is
conservative for whole-grain / sprouted / protein / gluten-free breads and
granola; vitamin-D milks are animal by-product; kosher is certified-or-inherent
with uncertain = not kosher; bone-char only on the plain-sugar staple; nuts
include "may contain" for generic chocolate, granola, baked goods and ice
cream.

Run from the repo root::

    python probe/2026-11-24/label.py            # writes labelled.json + the draft CSV

Nothing here touches the database.
"""
from __future__ import annotations

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from app.services.products import ATTRIBUTES  # noqa: E402

LETTER = {
    "m": "contains_meat", "f": "contains_fish", "d": "contains_dairy",
    "e": "contains_egg", "n": "contains_nuts", "a": "contains_animal_byproduct",
    "g": "contains_gluten", "k": "not_kosher",
}

#: Existing confirmed products I link to, with their CURRENT stored attributes
#: (copied from Neon on 2026-11-24; the proposer ignores attributes on a match,
#: they are here so the draft shows what each name will be judged by).
EXISTING = {
    "chicken breast": "mk", "cooked chicken breast": "mgk",
    "breaded chicken breast": "mdegk", "chicken thighs": "mk",
    "chicken drumsticks": "mk", "chicken breast bites": "mgk",
    "breaded white fish fillets": "fdegk", "breaded calamari": "fdegk",
    "salmon fillet": "f", "eggs": "e", "tofu": "k", "ground beef": "mk",
    "pork chops": "mk",
    "milk": "da", "lactose-free milk": "dak", "chocolate milk": "dak",
    "flavoured milk drink": "dak", "milkshake": "dgk", "evaporated milk": "dak",
    "condensed milk": "dk", "cream": "dk", "buttermilk": "dk", "butter": "dk",
    "cheddar cheese": "dk", "cheddar cheese slices": "dk",
    "cream cheese": "dk", "greek yogurt": "dk", "ice milk": "dk",
    "broccoli": "", "carrots": "", "yellow onion": "", "tomatoes": "",
    "cherry tomatoes": "", "roma tomatoes": "", "white mushrooms": "",
    "russet potatoes": "", "sweet potatoes": "", "spinach": "",
    "romaine lettuce": "", "cucumbers": "", "avocados": "",
    "canned tomatoes": "k", "strained tomatoes": "k",
    "sliced bread": "gk", "bagels": "gk", "whole grain bread": "agk",
    "protein bread": "agk", "protein bagels": "gk", "tortillas": "gk",
    "rye bread": "gk", "sourdough bread": "gk", "italian bread": "gk",
    "ciabatta": "gk", "buns": "gk", "hamburger buns": "gk",
    "english muffins": "gk", "bavarian bread": "gk", "texas toast": "dgk",
    "pita bread": "gk", "raisin bread": "dgk", "cinnamon raisin bread": "dgk",
    "pull-apart bread": "degk", "gluten-free bread": "deak",
    "gluten-free buns": "deak", "gluten-free rolls": "deak",
    "gluten-free pita bread": "deak", "gluten-free naan": "deak",
    "cheese ravioli": "mdengk", "mac and cheese": "dgk", "couscous": "gk",
    "granola": "dnagk", "peanut butter": "nk", "jam": "k",
    "coconut milk": "k", "coconut cream": "k", "non-dairy creamer": "ngk",
    "canned beans": "mk", "basmati rice": "", "all-purpose flour": "g",
    "granulated sugar": "a", "olive oil": "",
}

#: Owner-approved corrections to EXISTING confirmed products, applied through
#: the audited path (``product_corrections.correct_attributes``), AFTER the
#: labels are applied and committed; written to corrections.json for the
#: record. product_id -> (expected
#: canonical name, attribute changes, reason). The draft shows the corrected
#: value.
CORRECTIONS = {
    121: ("canned beans", {"contains_meat": False},
          "Plain canned legumes contain no meat; owner-approved correction "
          "2026-11-24. Pork-and-beans lines go to the separate baked beans "
          "product."),
}

#: New products this pass proposes: name -> (category, flags, pantry staple,
#: rationale shown in the draft).
NEW = {
    # --- seafood
    "raw shrimp": ("seafood", "fk", False, "shellfish; not a kosher species"),
    "cooked shrimp": ("seafood", "fk", False, "shellfish; not a kosher species"),
    "breaded shrimp": ("frozen", "fdegk", False,
                       "batter/breading: wheat, typically egg and milk; matches breaded white fish fillets"),
    "shrimp ring": ("seafood", "fgk", False,
                    "cooked shrimp with cocktail sauce; gluten conservative (sauce varies)"),
    "garlic shrimp": ("seafood", "fdgk", False,
                      "prepared garlic-butter shrimp; dairy and gluten conservative"),
    "shrimp skewers": ("seafood", "fgk", False, "marinated skewers; gluten conservative (soy-based marinades)"),
    "tilapia fillets": ("seafood", "fk", False,
                        "frozen processed fillets: kosher species but uncertified, so not kosher (only raw salmon is accepted by rule)"),
    "mackerel fillet": ("seafood", "fk", False, "packaged/smoked mackerel, uncertified"),
    "tuna steak": ("seafood", "fk", False, "frozen tuna steak, uncertified"),
    "surimi": ("seafood", "fegk", False, "imitation crab: fish, egg white, wheat starch"),
    "seaweed salad": ("prepared", "gk", False, "wakame with soy sauce (wheat) and sesame; no animal product"),
    "breaded scallops": ("frozen", "fdegk", False, "breaded shellfish; as breaded white fish fillets"),
    "bacon wrapped scallops": ("frozen", "mfk", False, "pork bacon + shellfish"),
    "fish cake stir-fry": ("prepared", "fegk", False, "fish cake: fish, egg white, wheat starch"),
    "shrimp paste on sugarcane": ("frozen", "fegk", False,
                                  "chao tom: shrimp paste, typically egg white and starch; gluten conservative"),
    # --- other proteins
    "mapo tofu": ("prepared", "mgk", False,
                  "the dish is tofu in a sauce with minced pork and doubanjiang (wheat); meat conservative"),
    "seasoned dried tofu": ("refrigerated", "gk", False,
                            "five-spice pressed bean curd braised in soy sauce (wheat); no animal product"),
    "plant-based egg substitute": ("refrigerated", "k", False,
                                   "JUST Egg: mung-bean protein, no egg or other animal product; certification not assumed"),
    "liquid egg whites": ("dairy", "ek", False, "pasteurised carton egg whites: processed, certification uncertain"),
    "salted duck egg yolk": ("pantry", "ek", False, "cured duck egg yolk, processed"),
    "marinated chicken breast": ("meat", "mdgk", False,
                                 "seasoned/marinated breast (asado roast, portions): dairy and gluten conservative"),
    "stuffed chicken breast": ("meat", "mdegk", False, "stuffing is typically cheese and breadcrumb"),
    "whole chicken": ("meat", "mk", False, "uncertified poultry (incl. halal)"),
    "kosher whole chicken": ("meat", "m", False, "sold as KOSHER: certified, so kosher"),
    "rotisserie chicken": ("deli", "mgk", False, "roasted/BBQ whole chicken: seasoning rub, gluten conservative"),
    "kosher ground beef": ("meat", "m", False, "sold as KOSHER: certified, so kosher"),
    "beef patties": ("meat", "mgk", False, "fresh patties: binder/seasoning possible, gluten conservative"),
    "ground beef and pork": ("meat", "mk", False, "mixed ground meat; pork"),
    "pork roast": ("meat", "mk", False, "pork"),
    "pork shoulder": ("meat", "mk", False, "pork; chops or roast of the same cut are one product"),
    "pork sirloin": ("meat", "mk", False, "pork; chops or roast of the same cut are one product"),
    "seasoned pork chops": ("prepared", "megk", False,
                            "Asian-grocer black-pepper / salt-and-pepper chops: battered or marinated, egg and gluten conservative"),
    "breaded pork cutlets": ("frozen", "mdegk", False, "breaded: wheat, egg, typically milk"),
    "deli turkey breast": ("deli", "mgk", False, "roasted/smoked deli turkey; matches cooked chicken breast"),
    "salami": ("deli", "mk", False, "Genoa salami: pork"),
    "refried beans": ("pantry", "mk", True,
                      "traditional refried beans commonly contain lard (meat); conservative"),
    "taco shells": ("pantry", "k", True, "corn shells; no tracked attribute"),
    "taco sauce": ("pantry", "gk", True, "gluten conservative (thickeners vary)"),
    # --- produce (fresh produce is inherently kosher by rule)
    "apples": ("produce", "", False, ""),
    "beets": ("produce", "", False, ""),
    "brussels sprouts": ("produce", "", False, ""),
    "celery": ("produce", "", False, ""),
    "cremini mushrooms": ("produce", "", False, ""),
    "oyster mushrooms": ("produce", "", False, "a mushroom, not shellfish"),
    "king oyster mushrooms": ("produce", "", False, "a mushroom, not shellfish"),
    "shiitake mushrooms": ("produce", "", False, ""),
    "seafood mushrooms": ("produce", "", False, "beech mushroom; 'seafood' is a varietal name"),
    "butternut squash": ("produce", "", False, ""),
    "green onions": ("produce", "", False, ""),
    "cilantro": ("produce", "", False, ""),
    "kale": ("produce", "", False, ""),
    "salad greens": ("produce", "", False, "bagged lettuce / leaf blends, undressed"),
    "mini potatoes": ("produce", "", False, ""),
    "potatoes": ("produce", "", False, "red/white/yellow potatoes; variants of one product"),
    "red onions": ("produce", "", False, ""),
    "bell peppers": ("produce", "", False, "mixed-colour sweet peppers"),
    "chinese broccoli": ("produce", "", False, ""),
    "chinese okra": ("produce", "", False, "luffa gourd"),
    "pomelo": ("produce", "", False, "'Red Honey' is a pomelo variety: fruit, no honey"),
    "seasoned breakfast potatoes": ("produce", "gk", False,
                                    "pre-cut potatoes with a seasoning: gluten conservative, processed"),
    "frozen vegetables": ("frozen", "k", False, "plain frozen vegetables; processed, certification uncertain"),
    "frozen fries": ("frozen", "gk", False, "coated fries (Flavour Crisp): coating may contain wheat"),
    "salad kit": ("prepared", "mfdengk", False,
                  "kits ship dressing/toppings: caesar (anchovy, parmesan, egg), bacon bits, nuts; conservative"),
    "canned fruit": ("pantry", "k", True, "fruit in juice/syrup"),
    "apple sauce": ("pantry", "k", True, ""),
    # --- bakery / starch
    "artisan bread": ("bakery", "gk", False, "lean artisan loaves; soft-bread rule, not dairy/egg"),
    "coconut bread": ("bakery", "degk", False, "Asian-bakery coconut bun: enriched with milk, butter, egg"),
    "vegan gluten-free bread": ("bakery", "k", False,
                                "Little Northern Bakehouse: certified vegan and gluten-free (no honey, egg or dairy)"),
    "challah": ("bakery", "eg", False, "MONTREAL KOSHER challah: egg-enriched, pareve, certified kosher"),
    "whole grain bagels": ("bakery", "agk", False, "whole-grain line: honey conservative per the honey rule"),
    "kosher pizza bagels": ("frozen", "dg", False,
                            "Macabee: a kosher-certified brand; mozzarella-type cheese (not a rennet-listed cheese)"),
    "protein tortillas": ("bakery", "agk", False, "protein line: honey conservative per the honey rule"),
    "gluten-free tortillas": ("bakery", "k", False, "BFree: certified gluten-free and vegan"),
    "lavash": ("bakery", "gk", False, ""),
    "naan": ("bakery", "degk", False, "naan is made with yogurt/milk and often egg"),
    "flatbread": ("bakery", "gk", False, "soft-bread rule"),
    # --- dairy and alternatives
    "brie cheese": ("dairy", "dk", False, "not a rennet-listed cheese"),
    "emmental cheese": ("dairy", "dk", False,
                        "not on the owner's rennet list (see 'for owner': imported Emmental is traditionally animal-rennet)"),
    "assorted cheese": ("dairy", "dk", False, "Gouda/Emmental-style assortment; not rennet-listed"),
    "kosher cream cheese": ("dairy", "d", False, "Givat: Israeli dairy sold kosher-certified"),
    "flavoured cream cheese rings": ("dairy", "dnk", False,
                                     "coated cheese rings (fruit/nut coatings): nuts conservative"),
    "cream cheese dip": ("dairy", "dgk", False, "dips: thickeners vary, gluten conservative"),
    "margarine": ("dairy", "dak", False,
                  "Lactantia margarine has milk ingredients; Canadian margarine is vitamin-D fortified (D3 rule)"),
    "vegan butter": ("dairy alternative", "nk", False,
                     "plant butter; some are cashew-based, nuts conservative"),
    "yogurt": ("dairy", "dak", False,
               "Activia / Mediterranee / Krema: no gelatin in these Canadian lines; carmine possible in fruit flavours"),
    "coconut kefir": ("dairy alternative", "k", False, "fermented coconut milk: plant-based; coconut is not a nut"),
    "filled fresh pasta": ("refrigerated", "mdengk", False,
                           "filled pasta: cheese (often parmesan = rennet), meat, pesto, egg dough; as cheese ravioli"),
    "fresh pasta": ("refrigerated", "mdengk", False,
                    "Rana / Ziggy's / O Sole Mio fresh lines are mostly filled pasta; conservative as cheese ravioli"),
    "gnocchi": ("pantry", "egk", True, "potato gnocchi: wheat, egg conservative"),
    # --- pantry
    "white rice": ("pantry", "", True, "plain rice is inherently kosher by rule"),
    "jasmine rice": ("pantry", "", True, "plain rice"),
    "long grain white rice": ("pantry", "", True, "plain rice"),
    "parboiled rice": ("pantry", "", True, "plain rice"),
    "medium grain rice": ("pantry", "", True, "plain rice (calrose / sushi / wuchang)"),
    "arborio rice": ("pantry", "", True, "plain rice"),
    "instant rice": ("pantry", "k", True, "precooked rice: processed, certification uncertain"),
    "seasoned microwave rice": ("pantry", "mdgk", True,
                                "Bistro Express varieties include chicken-flavoured and butter; conservative"),
    "shirataki rice": ("pantry", "k", True, "konjac, labelled GF"),
    "shirataki pasta": ("pantry", "gk", True, "konjac with oat fibre: oats not labelled GF"),
    "rice flakes": ("pantry", "k", True, "flattened rice: processed"),
    "rice noodles": ("pantry", "k", True, "rice sticks"),
    "sweet potato noodles": ("pantry", "k", True, "sweet potato starch vermicelli"),
    "glutinous rice flour": ("baking", "", True, "plain flour is inherently kosher by rule"),
    "corn masa flour": ("baking", "", True, "plain flour"),
    "whole wheat flour": ("baking", "g", True, "plain flour"),
    "atta flour": ("baking", "g", True, "durum whole-wheat flour"),
    "specialty flour": ("baking", "ngk", True,
                        "Bob's Red Mill range includes almond and other nut flours; conservative"),
    "oat flour": ("baking", "g", True, "oats not labelled GF"),
    "baking mix": ("baking", "dengk", True, "muffin/cookie mixes: milk, egg, may-contain nuts (baked goods)"),
    "rolled oats": ("pantry", "g", True, "oats not labelled GF; plain grain"),
    "gluten-free oats": ("pantry", "", True, "certified gluten-free oats (Stoked Oats, Only Oats); plain grain"),
    "oats with flax and chia": ("pantry", "k", True, "certified GF oat blend; mixed product, certification uncertain"),
    "instant oatmeal": ("pantry", "dagk", True,
                        "flavoured packets: some milk (peaches & cream), honey; see 'for owner'"),
    "protein instant oatmeal": ("pantry", "dagk", True, "protein from milk in most lines; honey possible"),
    "protein cereal": ("pantry", "dangk", True, "protein oat cereal: as granola"),
    "dry pasta": ("pantry", "gk", True, "durum semolina and water; certification uncertain"),
    "lasagna noodles": ("pantry", "egk", True, "oven-ready lasagne commonly contains egg"),
    "gluten-free pasta": ("pantry", "k", True, "corn/rice pasta"),
    "protein pasta": ("pantry", "egk", True, "semolina + legume flour; egg white in some lines, conservative"),
    "egg noodles": ("pantry", "egk", True, ""),
    "canned pasta": ("pantry", "mdgk", True,
                     "Chef Boyardee / Heinz canned pasta: ranges include beef ravioli and "
                     "spaghetti with cheese; meat and dairy conservative, wheat pasta"),
    "deli pasta salad": ("prepared", "mdengk", False,
                         "pasta salads: mayo (egg), parmesan, pesto, meat; conservative"),
    "deli salads": ("prepared", "mdegk", False, "potato/macaroni/coleslaw: mayo (egg), bacon in some; conservative"),
    "prepared pasta in sauce": ("prepared", "mdgk", False, "ready meal; cheese (parmesan possible) conservative"),
    "pasta sauce": ("pantry", "mdngk", True,
                    "brand-level jarred sauce: ranges include parmesan/romano (rennet = meat), meat, alfredo, pesto; see 'for owner'"),
    "kosher pasta sauce": ("pantry", "", True, "Gefen: kosher-certified brand; pareve tomato sauce"),
    "salsa": ("pantry", "k", True, ""),
    "guacamole": ("refrigerated", "k", False, "avocado dip, no animal product"),
    "tortilla chips": ("snacks", "k", True, "corn chips"),
    "olive pomace oil": ("pantry", "k", True, "refined (solvent-extracted) oil, like refined vegetable oil"),
    "truffle oil": ("pantry", "k", True, "flavoured oil"),
    "sun-dried tomatoes": ("pantry", "k", True, "processed (sulphited/dried)"),
    "dried shiitake mushrooms": ("pantry", "k", True, "dried, processed"),
    "dried chickpeas": ("pantry", "", True, "plain dry legumes are inherently kosher by rule"),
    "chicken bone broth": ("pantry", "mk", True, ""),
    "chicken broth mix": ("pantry", "mgk", True, "chicken fat/powder; hydrolysed wheat protein common"),
    "baked beans": ("pantry", "mgk", True,
                    "Heinz-style baked beans: the flyer line covers pork-and-beans and deep-browned "
                    "beans as well as plain beans in tomato sauce, so meat is conservative; "
                    "thickener/flavouring may contain wheat, gluten conservative"),
    "organic cane sugar": ("baking", "", True,
                           "organic cane sugar is not bone-char refined; sugars are inherently kosher by rule"),
}

#: Labels: index -> list of product names (existing or new). One element is a
#: single-product name; several is a multi-product tile.
L: dict[int, list[str]] = {}
NOTE: dict[int, str] = {}


def put(products, *idx, note=""):
    for i in idx:
        assert i not in L, f"index {i} labelled twice"
        L[i] = list(products) if isinstance(products, (list, tuple)) else [products]
        if note:
            NOTE[i] = note


# --- proteins ----------------------------------------------------------------
put("raw shrimp", 1, 4, 5, 9, 74, 75, 76, 77, 78, 79, 108, 109, 120, 121, 122,
    123, 124, 125, 126)
put(["raw shrimp", "tilapia fillets"], 2)
put(["refried beans", "tortillas", "taco shells", "taco sauce"], 3)
put(["raw shrimp", "breaded white fish fillets"], 6, 7, 8,
    note="sea salt & malt vinegar haddock bites: battered, malt = gluten")
put(["breaded white fish fillets", "breaded shrimp"], 10, 11, 59,
    note="High Liner range includes breaded/battered fish and shrimp; conservative superset")
put("breaded calamari", 12)
put("chicken breast", 13, 14, 16, 17, 18, 21, 22, 25, 26, 27, 29, 30, 31, 32)
put("breaded chicken breast", 15)
put("marinated chicken breast", 23, 24)
put("stuffed chicken breast", 28)
put(["chicken drumsticks", "chicken thighs"], 33, 36)
put("chicken thighs", 34, 35)
put("cooked shrimp", 37, 38, 39)
put(["cooked shrimp", "seaweed salad", "mackerel fillet"], 40, 41)
put("salted duck egg yolk", 42)
put("eggs", 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 54, 55, 56)
put("plant-based egg substitute", 53)
put("liquid egg whites", 57)
put(["fish cake stir-fry", "mapo tofu"], 58)
put("garlic shrimp", 60)
put("kosher ground beef", 61)
put("ground beef", 62, 63, 65, 66, 67, 69, 70, 71, 72, 73)
put("beef patties", 64)
put("ground beef and pork", 68)
put("breaded shrimp", 80)
put(["pork chops", "pork roast"], 81, 87)
put("seasoned pork chops", 82, 85)
put("pork chops", 83, 84)
put("breaded pork cutlets", 86)
put("pork shoulder", 88, 89)
put("pork sirloin", 90, 91, 92)
put("salmon fillet", 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105,
    note="salmon is a kosher species; raw salmon is accepted without a hechsher by rule")
put(["bacon wrapped scallops", "breaded scallops"], 106)
put("breaded scallops", 107)
put("shrimp ring", 110, 111)
put("shrimp skewers", 112)
put(["surimi", "raw shrimp", "cooked shrimp", "tuna steak"], 113)
put("tofu", 114, 116, 117)
put("mapo tofu", 115)
put("seasoned dried tofu", 118)
put(["deli turkey breast", "cooked chicken breast"], 119)
put(["whole chicken", "chicken drumsticks", "chicken thighs"], 127)
put("whole chicken", 128, 129, 131, 133, 134, 135, 136, 137, 139)
put("kosher whole chicken", 130)
put("rotisserie chicken", 132, 138)

# --- vegetables --------------------------------------------------------------
put(["beets", "carrots"], 140)
put(["beets", "carrots", "yellow onion"], 141)
put("bell peppers", 142)
put("broccoli", 143, 144)
put(["brussels sprouts", "broccoli"], 145)
put("white mushrooms", 146, 188, 232)
put(["carrots", "yellow onion"], 147, 148, 151, 152, 163)
put("carrots", 149, 150, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162)
put(["celery", "apples"], 164, 165)
put("tomatoes", 166, 229, 230, 262, 263, 264, 265, 267)
put(["cremini mushrooms", "white mushrooms"], 167, 222, 233, 234)
put(["butternut squash", "sweet potatoes"], 168)
put(["cucumbers", "yellow onion"], 169)
put(["cucumbers", "cherry tomatoes"], 170, 171)
put("frozen vegetables", 172, 173, 174, 175, 187)
put("green onions", 176)
put(["green onions", "cilantro"], 177)
put(["kale", "spinach"], 178)
put("salad greens", 179, 180, 181, 186)
put("mini potatoes", 182, 183, 184, 185, 211, 212)
put(["chinese okra", "broccoli"], 189)
put(["yellow onion", "red onions"], 190, 199)
put("yellow onion", 191, 192, 193, 195)
put("red onions", 194, 196, 197)
put(["red onions", "yellow onion"], 198, note="Spanish onions are large yellow onions")
put(["oyster mushrooms", "shiitake mushrooms"], 200)
put("king oyster mushrooms", 201)
put("russet potatoes", 202, 204, 210)
put("seasoned breakfast potatoes", 203)
put("potatoes", 205, 206, 207, 208, 209, 213, 214, 215)
put("romaine lettuce", 216, 217, 219)
put(["romaine lettuce", "salad kit"], 218)
put("seafood mushrooms", 220)
put("shiitake mushrooms", 221)
put(["white mushrooms", "cremini mushrooms", "broccoli"], 223)
put("spinach", 224)
put(["spinach", "chinese broccoli"], 225)
put(["bell peppers", "cherry tomatoes"], 226)
put("cherry tomatoes", 227)
put(["tomatoes", "brussels sprouts"], 228)
put("roma tomatoes", 231)

# --- fruit -------------------------------------------------------------------
put(["apples", "sweet potatoes"], 235)
put("apples", 236, 237, 238, 239, 240, 241, 242, 243, 244, 245, 247, 248, 249,
    250, 251, 252, 253, 254, 255, 256, 257, 258, 259, 260)
put(["apples", "avocados"], 246)
put(["canned fruit", "apple sauce"], 261)
put("canned tomatoes", 266, note="Longo's Curato is an imported line: canned San Marzano tomatoes")

# --- starch ------------------------------------------------------------------
put(["protein bagels", "protein bread", "protein tortillas"], 268)
put(["artisan bread", "ciabatta"], 269)
put("artisan bread", 270)
put("sliced bread", 271, 272, 302, 303)
put(["coconut bread", "whole grain bread"], 273, note="quinoa wheat bread: whole-grain, honey rule")
put(["whole grain bread", "sliced bread"], 274, 275,
    note="Country Harvest is a whole-grain line (honey rule); Farmhouse is plain sliced")
put("whole grain bread", 276, 279, 296)
put(["sliced bread", "buns"], 277)
put("rye bread", 278, 284, 290, 300, 301)
put("italian bread", 280, 291, note="Calabrese is an Italian lean loaf")
put("vegan gluten-free bread", 281)
put("challah", 282)
put(["gluten-free bread", "gluten-free buns", "gluten-free pita bread", "gluten-free naan"], 283)
put(["gluten-free bread", "gluten-free rolls"], 285)
put(["italian bread", "buns", "bagels"], 286, 287, 288)
put(["whole grain bread", "protein bread"], 289, note="fibre bread judged as whole-grain bread")
put("pull-apart bread", 292)
put("sweet potatoes", 293, 297)
put(["english muffins", "whole grain bread", "bavarian bread"], 294)
put(["frozen fries", "flatbread", "frozen vegetables"], 295)
put(["gluten-free pita bread", "gluten-free bread", "gluten-free buns"], 298)
put(["raisin bread", "cinnamon raisin bread"], 299)
put("sourdough bread", 304)
put(["tortillas", "pita bread", "lavash"], 305)
put(["tortillas", "naan"], 306)
put(["sliced bread", "whole grain bread"], 307, note="100% whole wheat judged whole-grain (honey rule)")
put(["sliced bread", "whole grain bread", "texas toast"], 308)

# --- dairy -------------------------------------------------------------------
put(["assorted cheese", "filled fresh pasta", "gnocchi"], 309)
put("brie cheese", 310, 311, 335)
put("butter", 312, 313, 314, 315, 316, 317, 318)
put("buttermilk", 319)
put("cheddar cheese", 320, 321, 322, 323, 324, 325, 326, 327, 328, 329)
put("coconut kefir", 330)
put("condensed milk", 332, 385)
put("flavoured cream cheese rings", 333)
put("cream cheese", 334, 338, 339, 340, 341, 342, 343, 344, 345, 346, 347, 348,
    349, 350, 351, 352, 353, 354, 355,
    note="lactose-free and flavoured cream cheese are still cream cheese")
put("kosher cream cheese", 336)
put(["cream cheese", "cream cheese dip"], 337)
put(["emmental cheese", "cheddar cheese"], 356)
put("flavoured milk drink", 357, 374, note="Milk2Go / flavoured milks: vitamin D3 rule")
put(["milkshake", "flavoured milk drink"], 358)
put(["greek yogurt", "yogurt"], 359, 361, 362)
put("greek yogurt", 360, 363, 364, 365, 366)
put("ice milk", 367)
put(["lactose-free milk", "cream cheese"], 368)
put(["margarine", "cream cheese"], 369)
put("milk", 371, 376, 377, 379, 380, 382, note="plain fluid milk: cholov stam rule (kosher), vitamin D3 rule")
put(["milk", "cream"], 372)
put(["milk", "cream", "chocolate milk"], 378)
put(["cheddar cheese slices", "brie cheese", "chicken breast bites", "salami"], 383)
put(["condensed milk", "evaporated milk"], 384)
put("vegan butter", 386)
put(["yogurt", "cream cheese"], 387)

# --- pantry staples and condiments --------------------------------------------
put("basmati rice", 388, 396, 397, 518)
put("arborio rice", 389)
put("whole grain bagels", 390, 391)
put("bagels", 392, 393)
put("kosher pizza bagels", 394, 395)
put("seasoned microwave rice", 398)
put("chicken bone broth", 399)
put(["baked beans", "canned pasta"], 401)
put("canned beans", 402, 403,
    note="plain canned legumes; canned beans (id 121) is corrected to contains_meat=false (owner-approved 2026-11-24, see CORRECTIONS)")
put("canned pasta", 404)
put("chicken broth mix", 405)
put("dried chickpeas", 406)
put(["coconut cream", "coconut milk"], 407)
put("coconut milk", 410, 411)
put("corn masa flour", 413)
put("canned tomatoes", 414)
put("dried shiitake mushrooms", 415, 416, 417, 418)
put("specialty flour", 419)
put("atta flour", 420, 421)
put("all-purpose flour", 422, 423, 424, 425, 426, 427, 428, 429, 430, 431)
put("whole wheat flour", 432)
put("fresh pasta", 433, 434, 435, 436, 437, 438)
put(["gluten-free pasta", "protein pasta"], 439)
put("glutinous rice flour", 440)
put("granola", 441)
put("instant oatmeal", 442, 443, 445)
put("protein instant oatmeal", 444, 446)
put("jasmine rice", 447, 448, 513, 536, 537)
put("parboiled rice", 449)
put(["long grain white rice", "parboiled rice"], 450)
put("long grain white rice", 451, 515)
put("medium grain rice", 400, 452, 453, 454, 516, 546)
put("egg noodles", 455)
put("whole grain bread", 456, note="oat bran / 7-grain / multigrain breads: whole-grain, honey rule")
put("protein cereal", 457)
put(["oat flour", "baking mix"], 458)
put("rolled oats", 459)
put("gluten-free oats", 460, 509, 510, 522)
put("oats with flax and chia", 461)
put("olive oil", 462, 463, 464, 465, 466, 467, 468, 469, 470, 471, 473, 474,
    476, 477, 478, 479, 480, 481)
put("olive pomace oil", 472, 475)
put(["strained tomatoes", "canned tomatoes"], 482)
put("strained tomatoes", 483)
put(["dry pasta", "couscous"], 484)
put(["dry pasta", "lasagna noodles"], 485)
put(["dry pasta", "mac and cheese"], 486)
put("dry pasta", 487, 488, 489, 490, 491, 493, 495, 496, 497, 498, 499, 500, 545)
put("shirataki pasta", 492, 524, note="Liviva makes konjac pasta with oat fibre")
put(["dry pasta", "strained tomatoes"], 494)
put(["deli pasta salad", "deli salads"], 501)
put("pasta sauce", 502, 503, 505, 506, 550, 551, 552, 553, 554, 555)
put("kosher pasta sauce", 504)
put("peanut butter", 507, 508)
put("cheese ravioli", 511)
put("white rice", 512)
put("instant rice", 514, 517, note="The Rice Stuff: precooked rice, judged instant")
put("rice flakes", 519)
put("gluten-free pasta", 520)
put("rice noodles", 521)
put(["sliced bread", "tortillas", "buns", "hamburger buns", "english muffins"], 523)
put("shirataki rice", 525)
put("organic cane sugar", 526, 528)
put("granulated sugar", 527, 529, 530, 531, note="plain sugar staple: bone-char rule")
put("pomelo", 532)
put("sun-dried tomatoes", 533)
put("sweet potato noodles", 534, 535)
put("gluten-free tortillas", 538)
put("tortillas", 539, 540, 542, 543, 544)
put("protein tortillas", 541)
put(["guacamole", "salsa", "tortilla chips"], 547)
put(["jam", "peanut butter"], 548)
put("non-dairy creamer", 549, note="Elmhurst creamers are nut/oat based")
put("prepared pasta in sauce", 556, 557)
put("shrimp paste on sugarcane", 558)
put("truffle oil", 559)

#: Pricing. A single product's flyer price is its own price, and an "A or B"
#: tile prices each item alike, so these are labelled per item (True). That is
#: a labeller decision, not a default: ``resolve_link_price_is_per_item`` still
#: turns it False for any deal whose own text shows a multi-buy ("2/", "2 for",
#: "or $3.69 ea."). These tiles join products with "and" or a bare list, so the
#: price may be a bundle: left undecided (None) for --reinfer-prices.
PRICE_UNDECIDED = {170, 190, 368}

#: Names that already carry a CONFIRMED alias whose price_is_per_item is NULL
#: (confirmed before migration 010), and whose deal text is undecidable, so
#: --reinfer-prices cannot fill it. Re-labelled to the SAME existing product:
#: the proposer's upsert only fills a NULL price (COALESCE) and leaves the
#: alias's confirmed status and the product's attributes untouched.
ALREADY_CONFIRMED = {19, 20, 331, 370, 373, 375, 381, 408, 409, 412}
put("chicken breast", 19, 20, note="existing confirmed alias; fills its NULL pricing only")
put("condensed milk", 331, note="existing confirmed alias; fills its NULL pricing only")
put("milk", 370, 373, 375, 381, note="existing confirmed alias; fills its NULL pricing only")
put("coconut milk", 408, 409, 412, note="existing confirmed alias; fills its NULL pricing only")


def flags_of(name: str, corrected: bool = False) -> str:
    if name in EXISTING:
        flags = EXISTING[name]
        if corrected:
            for _, (cname, changes, _) in CORRECTIONS.items():
                if cname == name:
                    for attr, value in changes.items():
                        letter = next(c for c, a in LETTER.items() if a == attr)
                        flags = (flags + letter) if value else flags.replace(letter, "")
        return flags
    return NEW[name][1]


def spec(name: str) -> dict:
    flags = flags_of(name)
    unknown = set(flags) - set(LETTER)
    assert not unknown, (name, unknown)
    attrs = {LETTER[c]: (c in flags) for c in LETTER}
    assert set(attrs) == set(ATTRIBUTES)
    if name in EXISTING:
        return {"match_existing_product": name, "canonical_name": name,
                "category": "existing", **attrs, "is_pantry_staple": False}
    category, _, staple, _ = NEW[name]
    return {"match_existing_product": None, "canonical_name": name,
            "category": category, **attrs, "is_pantry_staple": staple}


def build(names: list[dict]) -> tuple[list[dict], list[list[str]]]:
    by_i = {r["i"]: r for r in names}
    missing = sorted(set(by_i) - set(L))
    assert not missing, f"unlabelled indices: {missing}"
    extra = sorted(set(L) - set(by_i))
    assert not extra, f"labels for unknown indices: {extra}"
    labels, rows = [], []
    for i in sorted(L):
        raw = by_i[i]["name"]
        prods = L[i]
        for p in prods:
            assert p in EXISTING or p in NEW, (i, p)
        reason = NOTE.get(i) or "; ".join(
            NEW[p][3] for p in prods if p in NEW and NEW[p][3]) or "same product as the existing one"
        entry = {"raw_name": raw, "confidence": 0.9, "reason": reason, "unmappable": False}
        per_item = None if i in PRICE_UNDECIDED else True
        if len(prods) == 1:
            entry.update(spec(prods[0]))
            entry["price_is_per_item"] = per_item
        else:
            entry["products"] = [{**spec(p), "price_is_per_item": per_item} for p in prods]
        labels.append(entry)
        for pos, p in enumerate(prods):
            flags = flags_of(p, corrected=True)
            rows.append([
                i, raw, by_i[i]["role"], pos if len(prods) > 1 else "",
                p, "existing" if p in EXISTING else "NEW",
                *[("Y" if c in flags else "n") for c in "mfdenagk"],
                " | ".join(x for x in (NOTE.get(i), NEW[p][3] if p in NEW else "") if x),
            ])
    return labels, rows


def main() -> int:
    names = json.load(open(os.path.join(HERE, "names.json"), encoding="utf-8"))
    labels, rows = build(names)
    out = os.path.join(HERE, "labelled.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"labels": labels}, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(HERE, "corrections.json"), "w", encoding="utf-8") as fh:
        json.dump({"corrections": [
            {"product_id": pid, "canonical_name": name, "changes": changes, "reason": why}
            for pid, (name, changes, why) in sorted(CORRECTIONS.items())]}, fh, indent=1)
    csv_path = os.path.join(HERE, "draft.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["idx", "deal_name", "label_role", "tile_pos", "product",
                    "existing_or_new", "meat", "fish", "dairy", "egg", "nuts",
                    "animal_byproduct", "gluten", "not_kosher", "rationale"])
        w.writerows(rows)
    new_used = sorted({p for ps in L.values() for p in ps if p in NEW})
    unused = sorted(set(NEW) - set(new_used))
    print(f"{len(labels)} names labelled, {len(rows)} name-product rows")
    print(f"{len(new_used)} new products, "
          f"{len({p for ps in L.values() for p in ps if p in EXISTING})} existing reused")
    if unused:
        print("defined but unused:", unused)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
