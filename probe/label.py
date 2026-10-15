#!/usr/bin/env python
"""Build the labelled file for the 5-term probe.

I am the labeller (scripts/propose_products.py has no API client by design).
Entries are keyed by INDEX into tmp_probe/names.py's list, never by a retyped
string: three of the names differ only in invisible characters (U+00A0), and a
retyped raw_name would be rejected as "altered or invented".

Rulings applied, as the exported instructions require:
  * coconut is NOT a nut (none in this batch, but the rule is noted)
  * lactose-free milk IS dairy (none in this batch)
  * "Beefsteak Tomatoes" is a TOMATO -- "beef" is a varietal name
  * "Hamburger Buns" is BREAD -- the "ham" is incidental
  * attributes describe the FOOD, not the words in the string
  * contains_nuts is the allergy path, so it is labelled conservatively;
    where I am unsure, it is true and `reason` says so.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from names import selected  # noqa: E402

ATTRS = ("contains_meat", "contains_fish", "contains_dairy",
         "contains_egg", "contains_nuts")
LETTER = {"m": "contains_meat", "f": "contains_fish", "d": "contains_dairy",
          "e": "contains_egg", "n": "contains_nuts"}

ROWS = selected()
LABELS: dict[int, dict] = {}


def _attrs(flags: str) -> dict:
    on = {LETTER[c] for c in flags}
    return {a: (a in on) for a in ATTRS}


def _product(canon, cat, flags="", staple=False, match=None, per_item=None):
    return {
        "match_existing_product": match,
        "canonical_name": canon,
        "category": cat,
        **_attrs(flags),
        "is_pantry_staple": staple,
        "price_is_per_item": per_item,
    }


def one(idx, canon, cat, flags="", staple=False, conf=0.9, reason="",
        match=None, per_item=None):
    """A single-product entry (the common case)."""
    spec = _product(canon, cat, flags, staple, match, per_item)
    spec.pop("price_is_per_item")
    entry = {"raw_name": ROWS[idx - 1]["product_name"], **spec,
             "confidence": conf, "reason": reason, "unmappable": False}
    if per_item is not None:
        entry["price_is_per_item"] = per_item
    LABELS[idx] = entry


def multi(idx, parts, conf=0.8, reason=""):
    """A multi-product tile: a products list, one entry per product.

    price_is_per_item is left null throughout. A labeller reading the name
    alone genuinely cannot tell, and leaving it null is what gives
    scripts/review_products.py --reinfer-prices something to decide from the
    deal's own unit / sale_story / pre_price_text.
    """
    LABELS[idx] = {
        "raw_name": ROWS[idx - 1]["product_name"],
        "products": [_product(*p) if isinstance(p, tuple) else p for p in parts],
        "confidence": conf,
        "reason": reason,
        "unmappable": False,
    }


def bad(idx, reason, conf=0.3):
    """Unmappable. Attributes default to true (conservative) downstream."""
    LABELS[idx] = {"raw_name": ROWS[idx - 1]["product_name"],
                   "confidence": conf, "reason": reason, "unmappable": True}


BREAD = "bakery"
PROD = "produce"
PANTRY = "pantry"

# Plain lean yeast breads are labelled dairy-free: white/whole-wheat/sourdough/
# rye/pita/bagel doughs are flour-water-yeast-salt-sugar-oil and are usually
# vegan. Enriched and sweet breads (brioche, Hawaiian, milk, cheese, garlic,
# raisin, hot cross) are labelled dairy. Confidence reflects that this is a
# product-class judgement, not a read of an ingredient panel.
LEAN = 0.72
ENRICHED = 0.8

# ---------------------------------------------------------------- peanut butter
PB_R = "Peanut butter: peanuts are the product. contains_nuts true (peanut)."
one(4, "peanut butter", PANTRY, "n", True, 0.97, PB_R)
one(5, "peanut butter", PANTRY, "n", True, 0.97, PB_R, match="peanut butter")
one(26, "peanut butter", PANTRY, "n", True, 0.95,
    PB_R + " pre_price_text '2/' makes the $7.00 a two-for price, so this "
    "single product's price is NOT per item.", match="peanut butter",
    per_item=False)
one(80, "peanut butter", PANTRY, "n", True, 0.97, PB_R, match="peanut butter")
one(81, "peanut butter", PANTRY, "n", True, 0.97, PB_R, match="peanut butter")
one(82, "peanut butter", PANTRY, "n", True, 0.97, PB_R, match="peanut butter")
one(98, "peanut butter", PANTRY, "n", True, 0.97, PB_R, match="peanut butter")
one(130, "peanut butter", PANTRY, "n", True, 0.97, PB_R, match="peanut butter")
one(88, "peanut butter", PANTRY, "n", True, 0.93,
    "ONE product, not two. '750 G/ 1 KG' is a SIZE alternation (two jar "
    "sizes), not two products -- the multi-product detector's two size tokens "
    "are a false positive here.", match="peanut butter")
one(89, "peanut butter", PANTRY, "n", True, 0.93,
    "ONE product. '750 G/1 KG' is a size alternation, not two products.",
    match="peanut butter")
one(126, "peanut butter", PANTRY, "n", True, 0.93,
    "ONE product. '750 G/1 KG' is a size alternation, not two products.",
    match="peanut butter")
one(91, "peanut butter", PANTRY, "n", True, 0.9,
    "'Series' is a brand product line, not a distinct food. One product: "
    "peanut butter.", match="peanut butter")
one(138, "peanut butter", PANTRY, "n", True, 0.88,
    "ONE product. 'SELECTION OR LIFE SMART NATURALIA' is a BRAND alternation "
    "-- two store brands of the same food -- not two products. The detector "
    "flags the ' or ' and is wrong here.", match="peanut butter")
one(74, "peanut butter cups", "confectionery", "nd", False, 0.93,
    "Milk-chocolate confection with a peanut butter filling: nuts (peanut) "
    "and dairy (milk chocolate). Not a pantry staple -- it is candy.")
multi(86, [("peanut butter", PANTRY, "n", True, "peanut butter"),
           ("jam", PANTRY, "", True)],
      0.88,
      "Two unrelated products at one price: Kraft peanut butter and Bonne "
      "Maman jam. Three size tokens confirm a mashed tile.")
multi(87, [("peanut butter", PANTRY, "n", True, "peanut butter"),
           ("hazelnut spread", PANTRY, "nd", True)],
      0.85,
      "TWO products. Peanut butter and hazelnut spread are different foods, "
      "not variants. Both contain nuts; hazelnut spread also contains dairy "
      "(milk solids).")
multi(114, [("hazelnut spread", PANTRY, "nd", True, "hazelnut spread"),
            ("peanut butter", PANTRY, "n", True, "peanut butter")],
      0.85,
      "TWO products: Nutella hazelnut spread and peanut butter spread. "
      "Nutella contains hazelnuts and skim milk powder.")
multi(90, [("peanut butter", PANTRY, "n", True, "peanut butter"),
           ("processed cheese spread", PANTRY, "d", True)],
      0.88,
      "TWO unrelated products: Kraft peanut butter and Cheez Whiz. Cheez Whiz "
      "is a dairy cheese spread.")
one(105, "peanut butter snack bar", "snacks", "nd", False, 0.5,
    "Mid-Day Squares is a chocolate-coated peanut-butter-and-jam snack square. "
    "nuts true (PB&J). UNSURE on dairy: the brand markets plant-based bars but "
    "the chocolate coating may carry milk, so dairy is left true rather than "
    "risk a false safe label. A reviewer should check the panel.")

# ---------------------------------------------------------------------- tomatoes
TOM = "Fresh tomato. A vegetable (botanically a fruit); no animal products."
one(1, "cherry tomatoes", PROD, "", False, 0.85,
    "'Luscious Lovelies strawberry tomatoes' is a snacking-tomato varietal "
    "sold in a 340 g clamshell; 'strawberry' is the shape, not a fruit. "
    + TOM)
one(6, "cherry tomatoes", PROD, "", False, 0.85,
    "PC 'Tomato Cherry Mixiany' is a mixed snacking-tomato pack. " + TOM,
    match="cherry tomatoes")
one(10, "tomatoes", PROD, "", False, 0.9, "A 4-pack of fresh tomatoes. " + TOM)
one(29, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(47, "cherry tomatoes", PROD, "", False, 0.9,
    "'DRAMAS' is a snacking-tomato brand. " + TOM, match="cherry tomatoes")
one(59, "beefsteak tomatoes", PROD, "", False, 0.97,
    "A TOMATO. 'Beefsteak' is a varietal name describing the large meaty "
    "slicing tomato -- there is no beef and no animal product of any kind. "
    "This is the case the exported instructions call out, and it is exactly "
    "the one the substring oracle in app/agents/dietary.py gets wrong: 'beef' "
    "occurs inside 'beefsteak'.")
one(66, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(70, "tomatoes", PROD, "", False, 0.93,
    "Tomatoes on the vine. " + TOM, match="tomatoes")
one(76, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(77, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(78, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(95, "cherry tomatoes", PROD, "", False, 0.9, TOM, match="cherry tomatoes")
one(115, "cherry tomatoes", PROD, "", False, 0.9, TOM, match="cherry tomatoes")
one(117, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(118, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(119, "cherry tomatoes", PROD, "", False, 0.82,
    "'Apero' is a snacking-tomato varietal. " + TOM, match="cherry tomatoes")
one(120, "grape tomatoes", PROD, "", False, 0.9, TOM)
one(34, "grape tomatoes", PROD, "", False, 0.9, TOM, match="grape tomatoes")
one(123, "cherry tomatoes", PROD, "", False, 0.82,
    "PC 'Mixiany' is a mixed snacking-tomato pack. " + TOM,
    match="cherry tomatoes")
one(133, "tomatoes", PROD, "", False, 0.93,
    "Red vine tomatoes. " + TOM, match="tomatoes")
one(179, "tomatoes", PROD, "", False, 0.93,
    "Red vine tomatoes. " + TOM, match="tomatoes")
one(156, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(160, "tomatoes", PROD, "", False, 0.93, TOM, match="tomatoes")
one(159, "roma tomatoes", PROD, "", False, 0.9, "Roma (plum) tomatoes. " + TOM)
one(173, "cherry tomatoes", PROD, "", False, 0.8,
    "'Wild Wonders' is a branded mixed snacking-tomato line. " + TOM,
    match="cherry tomatoes")
one(172, "cherry tomatoes", PROD, "", False, 0.68,
    "ONE product. 'WILD WONDERS OR SWEET CHERRY TOMATOES' offers two labels "
    "of the same thing -- branded snacking tomatoes -- so it is a variant "
    "string, not a multi-product tile. " + TOM, match="cherry tomatoes")
one(100, "cherry tomatoes", PROD, "", False, 0.6,
    "ONE product, with low confidence. 'MEDLEY, CHERRY OR GRAPE TOMATOES' "
    "lists three snacking-tomato varietals at one price; by the exported "
    "test a shopper would call these the same thing with a different label. "
    "A reviewer may reasonably split them. " + TOM, match="cherry tomatoes")

CAN = ("Canned/jarred tomato product: shelf-stable, bought by the tin, so "
       "is_pantry_staple is true. No animal products.")
one(18, "canned tomatoes", PANTRY, "", True, 0.9, CAN)
one(23, "canned tomatoes", PANTRY, "", True, 0.9, CAN, match="canned tomatoes")
one(92, "canned tomatoes", PANTRY, "", True, 0.9, CAN, match="canned tomatoes")
one(103, "canned tomatoes", PANTRY, "", True, 0.9, CAN, match="canned tomatoes")
one(107, "canned tomatoes", PANTRY, "", True, 0.9, CAN, match="canned tomatoes")
one(109, "canned tomatoes", PANTRY, "", True, 0.9, CAN, match="canned tomatoes")
one(110, "canned tomatoes", PANTRY, "", True, 0.9, CAN, match="canned tomatoes")
one(108, "canned tomatoes", PANTRY, "", True, 0.9,
    CAN + " pre_price_text '2/' makes the $5.00 a two-for price, so it is NOT "
    "this product's own price.", match="canned tomatoes", per_item=False)
one(111, "canned tomatoes", PANTRY, "", True, 0.78,
    "ONE product. 'Polpa, Basil or Garlic' are seasoning variants of Mutti's "
    "canned tomatoes, not three products. " + CAN, match="canned tomatoes")
one(19, "strained tomatoes", PANTRY, "", True, 0.88,
    "Strained tomatoes (passata). " + CAN)
one(30, "strained tomatoes", PANTRY, "", True, 0.88,
    CAN + " pre_price_text '2/' makes the $5.00 a two-for price.",
    match="strained tomatoes", per_item=False)
one(33, "tomato paste", PANTRY, "", True, 0.93, CAN)
one(93, "tomato paste", PANTRY, "", True, 0.93, CAN, match="tomato paste")
one(99, "tomato paste", PANTRY, "", True, 0.93, CAN, match="tomato paste")
multi(104, [("strained tomatoes", PANTRY, "", True, "strained tomatoes"),
            ("canned tomatoes", PANTRY, "", True, "canned tomatoes")],
      0.78,
      "TWO products. Strained tomatoes (passata) and peeled/finely-chopped "
      "canned tomatoes are different pantry items a recipe is not "
      "interchangeable about. 'plain or with basil' and 'Peeled or Finely "
      "Chopped' are variants WITHIN each, and are not split further.")
multi(13, [("seasoned diced tomatoes", PANTRY, "", True),
           ("diced tomatoes with green chilies", PANTRY, "", True)],
      0.75,
      "TWO products at one price: Aylmer Accents (seasoned diced tomatoes) "
      "and Ro-Tel diced tomatoes with chilies. Different tins, two size "
      "tokens.")
multi(163, [("canned beans", PANTRY, "", True),
            ("canned tomatoes", PANTRY, "", True, "canned tomatoes")],
      0.85,
      "TWO products: Unico canned beans and canned tomatoes. Beans and "
      "tomatoes are not variants of each other.")
multi(164, [("canned tomatoes", PANTRY, "", True, "canned tomatoes"),
            ("canned beans", PANTRY, "", True, "canned beans")],
      0.85, "TWO products: Unico canned tomatoes and canned beans.")
one(97, "cheese ravioli", PANTRY, "de", False, 0.82,
    "Filled fresh pasta: asiago cheese filling (dairy) and egg pasta dough "
    "(egg). The sundried tomato is a flavouring. Refrigerated, so not a "
    "pantry staple.")
one(158, "tomato and grilled cheese soup", "prepared", "d", False, 0.78,
    "A prepared tomato soup sold with grilled cheese: contains dairy "
    "(cheese). This is a ready-meal, not a grocery staple -- a product class "
    "the catalogue has no other example of.")
one(157, "tomato and grilled cheese soup", "prepared", "d", False, 0.6,
    "The same item as 'Tomato and Grilled Cheese Soup' with the word 'Soup' "
    "truncated off by the provider. Labelled as the soup; dairy true.",
    match="tomato and grilled cheese soup")

# Produce tiles that pair tomatoes with something else.
multi(17, [("broccoli", PROD, "", False),
           ("spinach", PROD, "", False),
           ("tomatoes", PROD, "", False, "tomatoes")],
      0.88,
      "THREE products at one price: broccoli crowns, bunch spinach, tomatoes "
      "on the vine. All vegetables, no animal products. The unit 'ea/lb' is a "
      "spelling app.services.products._PER_ITEM_UNITS does not know.")
multi(73, [("grape tomatoes", PROD, "", False, "grape tomatoes"),
           ("cucumbers", PROD, "", False)],
      0.88,
      "TWO products: grape tomatoes and mini seedless cucumbers. The 'Product "
      "of ...' clauses are provenance text, not product names.")
multi(136, [("raspberries", PROD, "", False),
            ("cherry tomatoes", PROD, "", False, "cherry tomatoes")],
      0.88, "TWO products: raspberries and red cherry tomatoes.")
multi(137, [("roma tomatoes", PROD, "", False, "roma tomatoes"),
            ("green peppers", PROD, "", False)],
      0.88,
      "TWO products: roma tomatoes and green peppers, both by the pound.")
multi(60, [("avocados", PROD, "", False),
           ("tomatoes", PROD, "", False, "tomatoes")],
      0.85,
      "TWO products: a 5-pack of avocados and PC Balancia tomatoes. The "
      "multi-product detector MISSES this one -- no ' or ', and \"5's\" is "
      "not a size token it recognises.")

# ------------------------------------------------------------------ canned tuna
TUNA = ("Canned tuna: a fish, so contains_fish true. Shelf-stable tin, so "
        "is_pantry_staple true.")
one(112, "canned tuna", PANTRY, "f", True, 0.95, TUNA)
one(9, "canned tuna", PANTRY, "f", True, 0.8,
    TUNA + " The Korean varietal text '(라이트 스탠다드/고주)' is not readable "
    "from the tile, and '_150G*4 DW)' is a 4x150 g pack with stray provider "
    "punctuation, but the food itself is unambiguous: canned tuna. Not "
    "unmappable.", match="canned tuna")
one(31, "canned tuna", PANTRY, "f", True, 0.88,
    "ONE product. 'oil or water' is the PACKING MEDIUM -- a variant of the "
    "same chunk light canned tuna, not two products. The detector's ' or ' "
    "match is a false positive. pre_price_text '3/' makes the $5.00 a "
    "three-for price, so it is not this product's own price.",
    match="canned tuna", per_item=False)

# ------------------------------------------------------------------------- tofu
TOFU = ("Tofu is pressed soybean curd: no meat, fish, dairy, egg or nuts. "
        "is_pantry_staple is FALSE -- it is sold refrigerated in water and is "
        "perishable, so it is not a shelf-stable basic, even though "
        "app/services/flipp_staples.py files the search term under 'Pantry'.")
one(146, "tofu", "refrigerated", "", False, 0.93,
    TOFU + " The unit '2.87 /ea' is a per-item PRICE in the unit field, a "
    "spelling _PER_ITEM_UNITS cannot match.")
one(8, "tofu", "refrigerated", "", False, 0.93,
    TOFU + " pre_price_text '2/' makes the $4.98 a two-for price; the unit "
    "field even carries the real single price, 'or $2.89 each'.",
    match="tofu", per_item=False)
one(154, "tofu", "refrigerated", "", False, 0.93,
    TOFU + " pre_price_text 'from' means $3.49 is a floor across sizes, not "
    "this item's price.", match="tofu")
one(145, "tofu dessert", "refrigerated", "", False, 0.7,
    "Sunrise tofu dessert is a sweetened soy pudding, not plain tofu -- a "
    "different food, so it is not matched to 'tofu'. Soy-based, so no dairy. "
    "pre_price_text '2/' makes the $3.50 a two-for price.", per_item=False)
one(83, "roasted tofu", "refrigerated", "", False, 0.75,
    "Seasoned/roasted pressed tofu. 'Series' is a brand line, not a separate "
    "food. No animal products.")
one(58, "cheese tofu", "refrigerated", "d", False, 0.5,
    "UNSURE, labelled conservatively. 'Flower Cheese Tofu' is an East-Asian "
    "pressed-tofu product; some are soy-only with a cheese-like texture, "
    "others genuinely contain dairy cheese. dairy is left true so a vegan "
    "user is not shown it on a guess. A reviewer should check the panel.")

# ------------------------------------------------------------------------ bread
SLICED = ("Plain sliced yeast bread. Labelled dairy-free: standard Canadian "
          "white/whole-wheat sandwich loaves are flour, water, yeast, salt, "
          "sugar and oil. Perishable, so not a pantry staple.")
one(2, "sliced bread", BREAD, "", False, LEAN,
    "ONE product. 'White or Whole Wheat' are two flour variants of the same "
    "sandwich loaf, not two products. " + SLICED)
one(36, "sliced bread", BREAD, "", False, LEAN,
    "ONE product: white/whole-wheat variants of one farmhouse loaf. " + SLICED,
    match="sliced bread")
one(53, "sliced bread", BREAD, "", False, LEAN,
    "ONE product: white/whole-wheat variants. " + SLICED, match="sliced bread")
one(75, "sliced bread", BREAD, "", False, LEAN,
    "ONE product: a half loaf in white or whole wheat. " + SLICED,
    match="sliced bread")
one(176, "sliced bread", BREAD, "", False, LEAN,
    "ONE product: white/whole-wheat variants. " + SLICED, match="sliced bread")
one(39, "sliced bread", BREAD, "", False, LEAN, SLICED, match="sliced bread")
one(40, "sliced bread", BREAD, "", False, LEAN,
    "'Selected' is provider filler. " + SLICED, match="sliced bread")
one(42, "sliced bread", BREAD, "", False, 0.6,
    "'PS' is an unexplained provider suffix (likely a flyer page code), not "
    "part of the food. Read as Dempster's sliced bread. " + SLICED,
    match="sliced bread")
one(72, "sliced bread", BREAD, "", False, LEAN, SLICED, match="sliced bread")
one(113, "sliced bread", BREAD, "", False, LEAN, SLICED, match="sliced bread")
one(150, "sliced bread", BREAD, "", False, 0.68,
    "Stonemill 'Honest Wellness' is a seeded/sprouted-style sliced loaf. "
    + SLICED, match="sliced bread")
one(56, "sliced bread", BREAD, "", False, LEAN,
    SLICED + " pre_price_text '2/' makes the $6.00 a two-for price, so it is "
    "NOT this product's own price -- a SINGLE-product tile whose price is "
    "still not per-item.", match="sliced bread", per_item=False)
one(57, "sliced bread", BREAD, "", False, LEAN,
    SLICED + " pre_price_text '2/' makes the $7.00 a two-for price.",
    match="sliced bread", per_item=False)
one(170, "sliced bread", BREAD, "", False, 0.55,
    "LOW CONFIDENCE. 'VILLAGGIO BREAD DEMPSTER'S WHOLE GRAIN BREAD' is the "
    "same flyer tile as entries 167-169 with every size token stripped by the "
    "provider, which both hides the multi-product signal from the detector "
    "and drops the bagels. From this string alone only two sliced loaves are "
    "named, and both are sliced bread, so it is labelled as one product.",
    match="sliced bread")
one(25, "whole grain bread", BREAD, "", False, LEAN,
    "Whole grain sliced loaf. " + SLICED)
one(35, "whole grain bread", BREAD, "", False, LEAN, SLICED,
    match="whole grain bread")
one(45, "whole grain bread", BREAD, "", False, LEAN, SLICED,
    match="whole grain bread")
one(49, "protein bread", BREAD, "", False, 0.65,
    "Dempster's protein bread: added wheat/pea protein and seeds. No dairy "
    "or egg in the standard formulation; nuts false (seeds are not nuts). "
    + SLICED)
one(12, "bistro bread", BREAD, "", False, 0.72,
    "ACE Bakery bistro loaf -- a crusty artisan white bread. Dairy-free.")
one(20, "bistro bread", BREAD, "", False, 0.72,
    "A crusty artisan bistro loaf.", match="bistro bread")
multi(68, [("french bread", BREAD, "", False),
           ("italian bread", BREAD, "", False)],
      0.68,
      "TWO products. French and Italian loaves are different breads a shopper "
      "chooses between, not two labels for one thing. Both are lean "
      "flour-water-yeast doughs: no dairy, egg or nuts.")
multi(61, [("french bread", BREAD, "", False, "french bread"),
           ("italian bread", BREAD, "", False, "italian bread")],
      0.68, "TWO products: a French loaf or an Italian loaf, both 383 g.")
one(131, "pita bread", BREAD, "", False, 0.85,
    "Pita: flour, water, yeast, salt. No dairy, egg or nuts.")
one(27, "pita bread", BREAD, "", False, 0.85, "Pita flatbread.",
    match="pita bread")
one(106, "montreal style bagels", BREAD, "e", False, 0.7,
    "Montreal-style bagels contain EGG in the dough (and are honey-boiled), "
    "unlike New-York-style bagels. contains_egg true -- the word 'egg' does "
    "not appear in the string, which is exactly the kind of case the "
    "attributes exist to carry.")
one(134, "rye bread", BREAD, "", False, 0.78,
    "Rudolph's Bavarian rye: a rye sourdough loaf. No dairy, egg or nuts.")
one(116, "sprouted bread", BREAD, "", False, 0.75,
    "Sprouted-grain bread. Lean dough, no dairy or egg. Seeds are not nuts.")
one(139, "sprouted bread", BREAD, "", False, 0.75,
    "Silver Hills sprouted power bread.", match="sprouted bread")
one(140, "sprouted bread", BREAD, "", False, 0.75,
    "Silver Hills sprouted power bread; the same tile as entry 139 with the "
    "size range spaced differently.", match="sprouted bread")
one(149, "sprouted bread", BREAD, "", False, 0.75,
    "Silver Hills sprouted bread.", match="sprouted bread")
one(141, "sourdough bread", BREAD, "", False, 0.72,
    "ONE product. 'SOURDOUGH LOAF OR EVERYTHING SOURDOUGH BREAD' is a "
    "plain-or-topped variant of one sourdough loaf. Lean dough, dairy-free.")
multi(142, [("sourdough bread", BREAD, "", False, "sourdough bread"),
            ("pumpernickel bread", BREAD, "", False)],
      0.72,
      "TWO products. Sourdough and pumpernickel are different breads, not "
      "variants -- a shopper would not call them the same thing.")
multi(69, [("sourdough bread", BREAD, "", False, "sourdough bread"),
           ("pumpernickel bread", BREAD, "", False, "pumpernickel bread")],
      0.72, "TWO products: a sourdough loaf or a pumpernickel loaf.")
one(32, "cheese bread", BREAD, "d", False, 0.9,
    "Bread with cheese baked in: contains dairy.")
one(63, "garlic bread", BREAD, "d", False, 0.85,
    "Garlic bread is spread with garlic BUTTER: contains dairy.")
one(65, "garlic bread", BREAD, "d", False, 0.85,
    "Garlic butter: contains dairy.", match="garlic bread")
one(94, "garlic bread", BREAD, "d", False, 0.68,
    "ONE product. 'Artisan Garlic Bread or Cheese & Garlic Bread' are a plain "
    "and a cheese-topped version of one bake-at-home loaf. Both contain dairy "
    "(butter, and cheese on one variant), so the attributes are the same "
    "either way.", match="garlic bread")
one(67, "taralli bread sticks", PANTRY, "", False, 0.7,
    "Taralli are small baked Italian rings of flour, olive oil and wine -- no "
    "dairy, egg or nuts in the plain style. Shelf-stable in a box, but sold "
    "as a snack rather than a basic, so is_pantry_staple is left false.")
one(64, "gluten-free bread", BREAD, "e", False, 0.62,
    "Gluten-free loaves commonly use EGG as a binder, so contains_egg is true "
    "rather than risk a false safe label; a reviewer should check the brand. "
    "No dairy or nuts.")
one(128, "gluten-free bread", BREAD, "e", False, 0.62,
    "Gluten-free bread; egg binder assumed, see entry 64.",
    match="gluten-free bread")
one(132, "gluten-free bread", BREAD, "e", False, 0.62,
    "Gluten-free bread; egg binder assumed.", match="gluten-free bread")
one(37, "cranberry walnut bread", BREAD, "n", False, 0.93,
    "Contains WALNUTS: contains_nuts true. An artisan loaf with dried "
    "cranberries and walnuts.")
one(151, "raisin bread", BREAD, "d", False, 0.65,
    "Sun-Maid raisin bread is an enriched sweet loaf that carries milk "
    "ingredients: dairy true. UNSURE -- recorded so a reviewer checks it.")
one(152, "raisin bread", BREAD, "d", False, 0.65,
    "Sun-Maid raisin bread: enriched sweet loaf, dairy true (unsure).",
    match="raisin bread")
one(153, "raisin bread", BREAD, "d", False, 0.65,
    "Sun-Maid raisin sliced bread: enriched sweet loaf, dairy true (unsure).",
    match="raisin bread")
one(144, "cinnamon raisin bread", BREAD, "d", False, 0.62,
    "ONE product. 'CINNAMON RASIN' (provider typo for RAISIN) 'OR CINNABON "
    "BREAD' are two flavour labels on the same sweet cinnamon loaf. Enriched "
    "sweet dough, so dairy true (unsure).")
one(178, "soft milk bread", BREAD, "de", False, 0.6,
    "ONE product. 'SOFT CAKE/BREAD' is one Asian-bakery soft milk loaf "
    "described two ways, not two products -- the detector's two size tokens "
    "are a false positive. Soft Asian milk breads contain milk and egg.")
one(21, "breaded poultry", "frozen", "mde", False, 0.65,
    "Breaded poultry: meat (bird flesh), plus breading that standardly "
    "carries wheat, milk solids and egg wash -- the same attribute set the "
    "confirmed 'breaded chicken breast' product already uses. The name is "
    "generic flyer copy ('Boxed and Breaded Poultry') but the food is clear. "
    "pre_price_text '3/' makes the $20.00 a three-for price.", per_item=False)
bad(22, "UNMAPPABLE: 'Breaded Buttermilk' is a truncated name. The food "
        "cannot be identified -- it could be buttermilk-breaded chicken, "
        "fish, or a buttermilk loaf -- and at $14.99 under a 'bread' search "
        "it is most likely a frozen breaded protein. Withheld rather than "
        "guessed; attributes default to conservative (all true).", 0.25)

# Breaded fish, and the shellfish question.
one(16, "breaded white fish fillets", "frozen", "fde", False, 0.85,
    "Breaded haddock: a fish, so contains_fish true. The breading standardly "
    "carries milk solids and egg, so dairy and egg are true too.")
one(148, "breaded white fish fillets", "frozen", "fde", False, 0.85,
    "Seasoned breaded cod: fish, plus a breading with milk and egg.",
    match="breaded white fish fillets")
one(161, "breaded white fish fillets", "frozen", "fde", False, 0.62,
    "ONE product, with low confidence. 'Breaded or Battered Haddock or Cod' "
    "crosses two coatings with two white fish -- four labels on one "
    "freezer item a shopper treats as interchangeable. Splitting it would "
    "invent products; the attributes are identical either way.",
    match="breaded white fish fillets")
one(162, "breaded white fish fillets", "frozen", "fde", False, 0.62,
    "The same item as entry 161 with the promo tag \"Black Frid'Eh\" "
    "appended by the provider.", match="breaded white fish fillets")
one(96, "breaded calamari", "frozen", "fde", False, 0.9,
    "Calamari is SQUID -- a shellfish (mollusc). The exported rules fold "
    "shellfish into contains_fish, so contains_fish is true. Note that this "
    "loses the distinction a shellfish allergy actually needs: a user who "
    "can eat tuna but not squid has no way to say so. Breading adds dairy "
    "and egg.")
one(101, "paneer snacks", "frozen", "d", False, 0.72,
    "PANEER is Indian fresh cheese: contains_dairy true, contains_meat "
    "FALSE. 'BREADED BURGERS OR NUGGETS' sound like meat but these are "
    "vegetarian paneer products -- the mirror image of the Beefsteak Tomatoes "
    "case. Treated as one paneer snack line rather than three invented "
    "products.")
one(102, "paneer snacks", "frozen", "d", False, 0.72,
    "Paneer (Indian cheese) snacks: dairy true, meat false. 'Burgers' and "
    "'nuggets' do not imply meat here.", match="paneer snacks")
one(155, "granola", PANTRY, "n", True, 0.6,
    "UNSURE, labelled conservatively. Most granola contains tree nuts or is "
    "made on shared lines, so contains_nuts is true rather than risk exposing "
    "a nut-allergic user; this specific small-batch brand may be nut-free and "
    "a reviewer should check. Shelf-stable box: pantry staple.")

# Multi-product bakery tiles.
multi(3, [("sliced bread", BREAD, "", False, "sliced bread"),
          ("bagels", BREAD, "", False)],
      0.85,
      "TWO products: a Country Harvest loaf or bagels. Two size tokens. "
      "pre_price_text '2/' and unit 'or $4.29 ea.' both say the $7.50 covers "
      "two items.")
multi(28, [("sliced bread", BREAD, "", False, "sliced bread"),
           ("bagels", BREAD, "", False, "bagels")],
      0.85, "TWO products: a Country Harvest loaf or bagels.")
multi(7, [("gluten-free bread", BREAD, "e", False, "gluten-free bread"),
          ("gluten-free buns", BREAD, "e", False),
          ("gluten-free rolls", BREAD, "e", False),
          ("gluten-free pita bread", BREAD, "e", False),
          ("gluten-free english muffins", BREAD, "e", False),
          ("gluten-free naan", BREAD, "e", False)],
      0.6,
      "SIX products: Promise's gluten-free bread, buns, rolls, pitas, English "
      "muffins and naan at one price. These are different things, not labels "
      "for one thing, so the tile decomposes -- but note that one tile "
      "yielding six proposed products is how a 5-term probe inflates the "
      "catalogue. Egg assumed as a gluten-free binder throughout.")
multi(129, [("gluten-free bread", BREAD, "e", False, "gluten-free bread"),
            ("gluten-free buns", BREAD, "e", False, "gluten-free buns"),
            ("gluten-free rolls", BREAD, "e", False, "gluten-free rolls"),
            ("gluten-free pita bread", BREAD, "e", False,
             "gluten-free pita bread")],
      0.6, "FOUR products: Promise gluten-free bread, buns, rolls or pitas.")
multi(127, [("gluten-free bread", BREAD, "e", False, "gluten-free bread"),
            ("gluten-free buns", BREAD, "e", False, "gluten-free buns")],
      0.68,
      "TWO products, joined by 'AND' rather than 'OR' -- which is why the "
      "multi-product detector MISSES this tile: it looks only for ' or ' and "
      "for size tokens, and this string has neither.")
multi(11, [("bistro bread", BREAD, "", False, "bistro bread"),
           ("dinner rolls", BREAD, "", False)],
      0.8, "TWO products: an ACE Bakery bistro loaf or dinner rolls.")
multi(14, [("bagels", BREAD, "", False, "bagels"),
           ("pita bread", BREAD, "", False, "pita bread")],
      0.82,
      "TWO products: bagels (4's) or Cedar pita (5's). The detector MISSES "
      "this tile -- no ' or ', and \"4'S\"/\"5'S\" are not size tokens it "
      "recognises.")
multi(15, [("bagels", BREAD, "", False, "bagels"),
           ("pita bread", BREAD, "", False, "pita bread")],
      0.85, "TWO products: bagels or Cedar pita. Same tile as entry 14, "
            "rendered with 'OR' instead of the pack counts.")
multi(24, [("texas toast", BREAD, "d", False),
           ("garlic parmesan breadsticks", BREAD, "d", False),
           ("brioche bread", BREAD, "de", False),
           ("buns", BREAD, "", False)],
      0.68,
      "FOUR products: garlic Texas toast, garlic parmesan breadsticks, "
      "brioche bread, and buns. Texas toast and the breadsticks carry butter "
      "and parmesan (dairy); brioche is an egg-and-butter dough (dairy, egg). "
      "\"Black Frid'Eh\" is a promo tag, not a product. pre_price_text "
      "'starting at' means the $4.99 is a FLOOR across the tile -- the same "
      "semantics as 'from', which infer_price_is_per_item handles while "
      "'starting at' falls through undecided.")
multi(71, [("texas toast", BREAD, "d", False, "texas toast"),
           ("garlic parmesan breadsticks", BREAD, "d", False,
            "garlic parmesan breadsticks"),
           ("brioche bread", BREAD, "de", False, "brioche bread"),
           ("buns", BREAD, "", False, "buns")],
      0.68,
      "FOUR products -- the same tile as entry 24 without the promo tag. "
      "pre_price_text 'starting at' again.")
multi(38, [("sliced bread", BREAD, "", False, "sliced bread"),
           ("buns", BREAD, "", False, "buns")],
      0.82, "TWO products: a D'Italiano loaf or buns.")
multi(41, [("sliced bread", BREAD, "", False, "sliced bread"),
           ("buns", BREAD, "", False, "buns"),
           ("english muffins", BREAD, "", False)],
      0.85,
      "THREE products: bread 650 g, buns 410 g, English muffins 375 g. Three "
      "size tokens -- the high-confidence multi-product signal.")
multi(43, [("english muffins", BREAD, "", False, "english muffins"),
           ("dinner rolls", BREAD, "", False, "dinner rolls"),
           ("bavarian bread", BREAD, "", False)],
      0.85,
      "THREE products from three brands: Dempster's English muffins, "
      "Compliments dinner rolls, Rudolph's Bavarian bread.")
multi(44, [("sliced bread", BREAD, "", False, "sliced bread"),
           ("buns", BREAD, "", False, "buns")],
      0.8,
      "TWO products: white/whole-wheat loaves (variants of one bread) or "
      "signature buns. pre_price_text is '2 for', which means the $6.00 "
      "covers two items -- and neither _QTY_PREFIX_RE (which only matches "
      "'N/') nor _MULTIBUY_RE (which is only applied to sale_story) catches "
      "it, so the code leaves it undecided.")
multi(46, [("whole grain bread", BREAD, "", False, "whole grain bread"),
           ("bagels", BREAD, "", False, "bagels")],
      0.82,
      "TWO products joined by ', AND' -- another tile the detector MISSES "
      "because it only looks for ' or ' and size tokens.")
multi(48, [("hamburger buns", BREAD, "", False),
           ("sliced bread", BREAD, "", False, "sliced bread"),
           ("english muffins", BREAD, "", False, "english muffins")],
      0.85,
      "THREE products: hamburger buns, white/whole-wheat sliced bread, "
      "English muffins. 'Hamburger Buns' is BREAD -- the 'ham' is incidental, "
      "and it is the second substring-oracle false positive in this batch "
      "after Beefsteak Tomatoes.")
multi(50, [("protein bread", BREAD, "", False, "protein bread"),
           ("protein bagels", BREAD, "", False)],
      0.72,
      "TWO products: Dempster's protein bread or protein bagels. A loaf and "
      "a bagel are different things.")
multi(51, [("sliced bread", BREAD, "", False, "sliced bread"),
           ("bagels", BREAD, "", False, "bagels"),
           ("tortillas", BREAD, "", False),
           ("english muffins", BREAD, "", False, "english muffins"),
           ("snack cakes", BREAD, "de", False)],
      0.78,
      "FIVE products on one tile: sandwich bread, bagels, tortillas, English "
      "muffins and Vachon snack cakes. The snack cakes are an egg-and-dairy "
      "sponge. The widest decomposition in this probe.")
multi(52, [("english muffins", BREAD, "", False, "english muffins"),
           ("whole grain bread", BREAD, "", False, "whole grain bread"),
           ("snack cakes", BREAD, "de", False, "snack cakes")],
      0.8,
      "THREE products: sourdough English muffins, whole grain bread, Hostess "
      "snack cakes (egg and dairy sponge).")
multi(54, [("sliced bread", BREAD, "", False, "sliced bread"),
           ("buns", BREAD, "", False, "buns")],
      0.82, "TWO products: a white/whole-wheat loaf or buns.")
multi(55, [("whole grain bread", BREAD, "", False, "whole grain bread"),
           ("raisin bread", BREAD, "d", False, "raisin bread"),
           ("malt bread", BREAD, "d", False),
           ("tortillas", BREAD, "", False, "tortillas")],
      0.68,
      "FOUR products: whole grain/signature loaves (variants of one bread), "
      "raisin bread, malt bread, and tortillas. Raisin and malt breads are "
      "enriched sweet loaves, so dairy true (unsure).")
multi(79, [("sliced bread", BREAD, "", False, "sliced bread"),
           ("pull-apart bread", BREAD, "d", False)],
      0.75,
      "TWO products: Irresistible bread or Dempster's pull-aparts. "
      "Pull-apart loaves are buttered/cheesed, so dairy true.")
multi(84, [("hawaiian rolls", BREAD, "de", False),
           ("brioche bread", BREAD, "de", False, "brioche bread"),
           ("buns", BREAD, "", False, "buns"),
           ("raisin bread", BREAD, "d", False, "raisin bread")],
      0.78,
      "FOUR products: King's Hawaiian rolls, Irresistible brioche bread or "
      "buns, and Sun-Maid/Dempster's raisin bread. Hawaiian rolls and brioche "
      "are enriched milk-and-egg doughs.")
multi(85, [("hawaiian rolls", BREAD, "de", False, "hawaiian rolls"),
           ("brioche bread", BREAD, "de", False, "brioche bread"),
           ("buns", BREAD, "", False, "buns"),
           ("raisin bread", BREAD, "d", False, "raisin bread")],
      0.78, "FOUR products -- the same tile as entry 84 with sizes stripped.")
multi(121, [("cinnamon raisin bread", BREAD, "d", False,
             "cinnamon raisin bread"),
            ("hot cross buns", BREAD, "de", False)],
      0.8,
      "TWO products: cinnamon raisin bread or hot cross buns. Both enriched "
      "sweet doughs; hot cross buns are glazed with egg.")
multi(122, [("hot cross buns", BREAD, "de", False, "hot cross buns"),
            ("cinnamon raisin bread", BREAD, "d", False,
             "cinnamon raisin bread")],
      0.8, "TWO products: extra-raisin hot cross buns or extra-thick cinnamon "
           "raisin bread.")
multi(124, [("english muffins", BREAD, "", False, "english muffins"),
            ("sliced bread", BREAD, "", False, "sliced bread"),
            ("buns", BREAD, "", False, "buns"),
            ("bavarian bread", BREAD, "", False, "bavarian bread")],
      0.78,
      "FOUR products: PC/Blue Menu English muffins, Wonder bread, buns, and "
      "Rudolph's Bavarian bread.")
multi(125, [("english muffins", BREAD, "", False, "english muffins"),
            ("sliced bread", BREAD, "", False, "sliced bread"),
            ("buns", BREAD, "", False, "buns"),
            ("bavarian bread", BREAD, "", False, "bavarian bread")],
      0.78, "FOUR products -- the same tile as entry 124, punctuated "
            "differently by the provider.")
multi(135, [("sliced bread", BREAD, "", False, "sliced bread")],
      0.6,
      "ONE product, kept as a products list only because the string reads as "
      "an alternation. \"RUDOLPH'S OR PREMIERE MOISSON BREAD\" is a BRAND "
      "alternation -- two bakeries' loaves -- not two foods. The detector's "
      "' or ' match is a false positive.")
multi(143, [("raisin bread", BREAD, "d", False, "raisin bread"),
            ("malt bread", BREAD, "d", False, "malt bread")],
      0.75,
      "TWO products: Sun-Maid/Dempster's raisin bread or malt bread. Both "
      "enriched sweet loaves (dairy true, unsure).")
multi(147, [("custard puff bread", BREAD, "de", False),
            ("soft milk bread", BREAD, "de", False, "soft milk bread")],
      0.78,
      "TWO products separated by a '/': salted-egg-yolk custard puff bread "
      "and milk bread. Both contain egg and dairy (salted egg yolk, custard, "
      "milk). The detector MISSES this one -- '/' is not a separator it "
      "looks for. pre_price_text '2/' makes the $5.70 a two-for price, and "
      "sale_story 'Mix & Match' is a multi-buy idiom _MULTIBUY_RE does not "
      "match because it carries no quantity.")
multi(165, [("snack cakes", BREAD, "de", False, "snack cakes"),
            ("pastries", BREAD, "de", False),
            ("rye bread", BREAD, "", False, "rye bread")],
      0.72,
      "THREE products: Vachon/Hostess snack cakes, pastries, and Natural "
      "Bakery Canadian rye bread. Note the provider's doubled 'OR OR' in the "
      "raw string. Snack cakes and pastries are egg-and-dairy baked goods.")
multi(166, [("sliced bread", BREAD, "", False, "sliced bread"),
            ("dinner rolls", BREAD, "", False, "dinner rolls")],
      0.78, "TWO products: Villa Dimanno bread or rolls.")
multi(167, [("sliced bread", BREAD, "", False, "sliced bread"),
            ("whole grain bread", BREAD, "", False, "whole grain bread"),
            ("bagels", BREAD, "", False, "bagels")],
      0.85,
      "THREE products: Villaggio bread, Dempster's whole grain bread, bagels. "
      "Three size tokens.")
multi(168, [("sliced bread", BREAD, "", False, "sliced bread"),
            ("whole grain bread", BREAD, "", False, "whole grain bread"),
            ("bagels", BREAD, "", False, "bagels")],
      0.85, "THREE products -- the same tile as entry 167 with a comma added.")
multi(169, [("sliced bread", BREAD, "", False, "sliced bread"),
            ("whole grain bread", BREAD, "", False, "whole grain bread"),
            ("bagels", BREAD, "", False, "bagels")],
      0.85,
      "THREE products -- byte-for-byte the same visible text as entry 167, "
      "differing only by a U+00A0 non-breaking space. The provider renders "
      "one tile as several distinct product_name values.")
multi(171, [("italian bread", BREAD, "", False, "italian bread"),
            ("buns", BREAD, "", False, "buns"),
            ("sliced bread", BREAD, "", False, "sliced bread")],
      0.7,
      "THREE products: Villaggio Italian style breads, buns, and Country "
      "Harvest breads (a plain sliced loaf). pre_price_text '2 for' with unit "
      "'or $3.99 ea.' says the $7.00 covers two -- a shape the code leaves "
      "undecided.")
multi(174, [("sliced bread", BREAD, "", False, "sliced bread"),
            ("buns", BREAD, "", False, "buns")],
      0.85, "TWO products: Wonder bread 675 g or buns 8's.")
multi(175, [("tortillas", BREAD, "", False, "tortillas"),
            ("sausage buns", BREAD, "", False),
            ("sliced bread", BREAD, "", False, "sliced bread")],
      0.7,
      "THREE products: Wonder wraps, D'Italiano sausage buns, and bread. The "
      "'SAUSAGE BUNS' are the BUN SHAPE for a sausage -- no meat in the bun, "
      "so contains_meat is false. The substring oracle will disagree: "
      "'sausage' is in its forbidden list.")
multi(177, [("sliced bread", BREAD, "", False, "sliced bread"),
            ("texas toast", BREAD, "d", False, "texas toast")],
      0.7,
      "TWO products: white/whole-wheat sandwich bread (variants of one loaf) "
      "and Texas toast, which is buttered (dairy).")
multi(62, [("ciabatta", BREAD, "", False),
           ("country bread", BREAD, "", False),
           ("baguette", BREAD, "", False)],
      0.75,
      "THREE products: ciabatta, rustic country breads, baguettes. All lean "
      "artisan doughs: no dairy, egg or nuts.")


def main() -> int:
    missing = [i for i in range(1, len(ROWS) + 1) if i not in LABELS]
    if missing:
        print(f"UNLABELLED indices ({len(missing)}): {missing}")
    extra = [i for i in LABELS if not 1 <= i <= len(ROWS)]
    if extra:
        print(f"BAD indices: {extra}")
        return 1

    out = {"labels": [LABELS[i] for i in sorted(LABELS)]}
    path = "/tmp/probe_labelled.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)

    n_multi = sum(1 for e in out["labels"] if "products" in e)
    n_prod = sum(len(e.get("products", [1])) for e in out["labels"])
    print(f"labelled {len(out['labels'])} of {len(ROWS)} name(s)")
    print(f"  multi-product entries : {n_multi}")
    print(f"  product rows produced : {n_prod}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
