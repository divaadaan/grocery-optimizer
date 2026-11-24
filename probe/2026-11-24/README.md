# Product-linking pass, 2026-11-24

Live deals for the test postal code (`TEST_POSTAL_CODE`, never written here)
that feed the trays, labelled and linked to confirmed products. Reviewer for
every machine decision: `claude-review-2026-11-24`. The owner pre-approved
the Neon writes for this pass on 2026-11-24; this file and `draft.csv` are
the draft to review afterwards.

## Files

| File | What it is |
|---|---|
| `names.json` | Frozen list of the unmapped, currently valid deal names worked on, with their stored `deal_labels` role/item. Role/item decided only **which names came first** (anchors and sides, then stock-up). |
| `label.py` | The labelling pass: every name, by index, to existing or new products, with attributes and rationale. Regenerates the two files below. |
| `labelled.json` | `propose_products.py --import` format, the input to `scripts/apply_product_review.py`. |
| `corrections.json` | Owner-approved correction to an existing confirmed product (`canned beans`, id 121, `contains_meat` → false), applied through `product_corrections.correct_attributes` after the labels were committed. |
| `draft.csv` | The table for review: one row per (deal name, product). Columns: index, deal name, label role, tile position, product, existing/NEW, the eight attributes (Y/n), rationale. |

Every attribute is my judgement of the product as sold in Canadian flyers,
following the owner's labelling rules. Nothing was derived from a name in
code. No product confirmed by this pass has a NULL attribute (the apply
script refuses otherwise).

## Scope

The pass covered 559 unmapped deal names, current as of 2026-11-24, whose stored role is a tray role:
- 139 protein (anchors)
- 248 vegetable, fruit, starch and dairy (sides)
- 172 pantry staple and condiment (stock-up)

Non-food items, beverages, snacks and prepared meals were left unmapped. No name was marked unmappable.

| | |
|---|---|
| names labelled | 559 (687 name→product rows; 100 multi-product tiles) |
| new products | 138 |
| existing products reused | 81 |
| aliases confirmed | 646 on 219 products, plus 10 older confirmed aliases whose NULL pricing was filled |
| deal-product rows linked (all stores) | 746 |
| correction | `canned beans` (id 121) `contains_meat` true → false, correction_id 378 |

Pricing: single products and "A or B" tiles are labelled per item. The
per-deal multi-buy check (`resolve_link_price_is_per_item`) still turns a
deal to bundle pricing when its own text says "2/", "2 for" or "or $x ea.".
Three tiles that join products with "and" or a bare list were left to
`--reinfer-prices`. Pricing is report-only.

### Before / after (test postal code, 916 current deals)

Linked deals: **37 → 644** (all to confirmed products).

| diet | gated deals | anchors | sides | stock-up |
|---|---|---|---|---|
| unrestricted | 916 → 916 | 15 → 15 | 25 → 25 | 15 → 15 |
| vegetarian | 32 → 483 | 1 → 4 | 12 → 25 | 1 → 15 |
| vegan | 21 → 333 | 1 → 2 | 6 → 25 | 1 → 15 |
| kosher | 12 → 269 | 0 → 4 | 4 → 25 | 0 → 8 |

The after figures include the canned-beans correction, which put canned beans into the
vegetarian and vegan stock-up trays.

What the anchor counts say:
- Vegetarian anchors are tofu, eggs, egg whites and salted duck egg yolk.
- Vegan anchors are tofu and JUST Egg.
- Kosher anchors are eggs, salmon fillet, kosher whole chicken and kosher ground beef.

The vegetarian and vegan anchor trays stay small because this week's flyers carry few
non-meat proteins labelled `protein`. Legumes are labelled `pantry_staple`, so
they reach stock-up rather than anchors. That is a `deal_labels` role
question, not a linking one.

Surfacing quirk seen: the vegan stock-up "sugar" item is the honey pomelo
deal. Its stored `deal_labels` item/role is sugar/pantry_staple, but its
product attributes are right (fresh fruit).

### Correction impact (`canned beans`, id 121)

Before the correction, the only aliases on 121 were Unico (×2), Primo and SELECTION; no Heinz
or pork-and-beans alias. Impact:
- 6 linked deals (4 live) newly servable for vegetarian, vegan and pescatarian; newly withheld for nobody.
- Active restricted users: user 1 (vegetarian) and user 4 (vegan) each gain deals 647, 648, 1174 and 1175, and lose none.

## For owner: borderline calls with large reach impact

1. **Brand-level `pasta sauce` is `meat, dairy, nuts, gluten, not kosher`**
   (10 names, the main stock-up sauce). Classico, Rao's, PC, Primo, Bertolli
   and the rest sell four-cheese, alfredo, vodka-cream, pesto and meat sauces
   under one flyer line. Romano/parmesan count as meat under the rennet rule,
   so a generic jar fails vegetarian. If you would rather link plain
   marinara/tomato lines separately, they need distinct products, and the
   flyer string rarely says which jar it is. Gefen (a kosher brand) got its own
   `kosher pasta sauce` with no flags.
2. **`canned beans` (id 121) corrected to `contains_meat=false` (owner-approved
   2026-11-24).** It was stored as meat although its aliases are Unico plain
   canned beans. Primo and SELECTION canned beans are linked to it too. The
   correction went through `product_corrections.correct_attributes` (audited,
   reviewer `claude-review-2026-11-24`) after the labels were committed; its
   impact report is below. The Heinz "beans or pasta" tile is **not** on
   product 121. Heinz Canada sells pork-and-beans and deep-browned beans under
   the same flyer line, so it goes to a new `baked beans` product (meat,
   gluten, not kosher, all conservative) plus `canned pasta`.
3. **`instant oatmeal` / `protein instant oatmeal` are `dairy, by-product, gluten, not kosher`.**
   Flavoured packet assortments include milk (peaches & cream) and honey
   varieties. Plain oats are linked separately: `rolled oats` (gluten), and
   `gluten-free oats` (no flags, inherently kosher) for Stoked Oats and Only
   Oats.
4. **`fresh pasta` (Rana, Ziggy's, O Sole Mio, Compliments) is labelled like
   the existing `cheese ravioli`: `meat, dairy, egg, nuts, gluten, not kosher`.**
   These fresh lines are mostly filled pasta (parmesan, meat, pesto). This
   removes every fresh pasta from vegetarian reach.
5. **Imported Emmental and West Country Farmhouse Cheddar are *not* meat.**
   I followed your closed rennet list (parmesan, grana, pecorino/romano,
   gruyère, asiago; other cheeses are false). Both are traditionally made
   with animal rennet. Adding them would remove 2 names from vegetarians.
6. **Kosher grants made from the label or a kosher-only brand:** `kosher
   ground beef` (CONTINENTAL ... KOSHER BEEF), `kosher whole chicken`,
   `challah` (MONTREAL KOSHER), `kosher pizza bagels` (Macabee, both strings),
   `kosher cream cheese` (Givat), `kosher pasta sauce` (Gefen). ZETA KOSHER
   olive oil went to the existing olive oil, which is already kosher. Fresh
   salmon fillets (13 names) went to the existing `salmon fillet`, which is
   kosher by your raw-salmon rule. Frozen tilapia, mackerel and tuna were
   *not* granted.
7. **Vegan grants by brand:** Little Northern Bakehouse → `vegan gluten-free
   bread`, BFree → `gluten-free tortillas`, JUST Egg → `plant-based egg
   substitute` (a vegan anchor), Riviera coconut kefir.
8. **`yogurt` (Activia, Méditerranée, Krema) is not meat.** These Canadian
   lines use no gelatin. Some other Canadian yogurts do, and none of them are
   in this pass.

9. **Existing `non-dairy creamer` clears vegan (not changed).** That is right
   for Elmhurst (nut/oat based), which this pass links to it. The product is
   generic, though, and many "non-dairy" creamers (Coffee-mate and similar)
   contain sodium caseinate, a milk protein. If one is ever linked to this
   product it would wrongly clear vegan and dairy-free. A safer split would
   be a plant-based creamer product for Elmhurst-type lines.
10. **Existing `protein bagels` (Dempster's) stores `animal_byproduct=false`
    (not changed).** Your honey rule makes protein breads animal by-product
    true, and the sibling `protein bread` already is. This pass links one tile
    (protein bagels, bread and tortillas) to it. Correcting it would remove
    protein bagels from vegan reach.

## Smaller conservative calls (not expected to matter much)

Breaded and battered seafood follows the existing `breaded white fish
fillets` (fdegk). `mapo tofu` counts as meat: the dish has minced pork.
`refried beans` counts as meat: Old El Paso traditional uses lard. Salad kits
get every flag. `margarine` counts as dairy and by-product (milk ingredients,
mandatory vitamin D). `vegan butter` counts as nuts (some are cashew-based).
`frozen vegetables`, dried mushrooms, instant rice and other processed plain
foods count as not kosher, because certification is uncertain. Plain rice,
flour, dried chickpeas and produce are inherently kosher by rule.
