-- 015_contains_animal_byproduct.sql
-- Why: "vegan" was meat + fish + dairy + egg all false, so honey and the other
--   animal products that are none of those passed for vegans. 014 had to leave
--   Honey out of the pantry staples for exactly that reason. This adds the
--   missing fact as its own column, read by products.RESTRICTION_ATTRIBUTES
--   for "vegan" only (vegetarians and pescatarians eat honey).
-- Design log: (new) "contains_animal_byproduct: the vegan-only attribute".
-- Hernandez: single-field-value rule -- "contains an animal-derived ingredient
--   that is not meat, fish, dairy or egg" is one fact about the product.
--
-- Labelling rule (also in products.ATTRIBUTE_RULES, the proposer prompt and
--   both review surfaces): contains_animal_byproduct covers honey, beeswax,
--   carmine/cochineal, shellac/confectioner's glaze, bone-char-refined sugar,
--   lanolin-derived vitamin D3. Slaughter by-products that vegetarians also
--   avoid -- gelatin, lard, tallow, animal rennet -- are contains_meat, NOT
--   this column, so they keep failing vegetarian and pescatarian.
--
-- NULLABLE, unlike the five columns from 006. Every product confirmed before
--   this migration was reviewed without this question being asked, so there
--   is no honest value to give it: NULL means "undecided" and FAILS CLOSED
--   (the gate tests "IS NOT FALSE", product_allows requires an explicit
--   False). Consequence: those products are withheld from vegans until a
--   reviewer decides them -- `scripts/review_products.py --fill-undecided` or
--   the "undecided attribute" card at /admin/review. No backfill guesses
--   false for them. Rows already failing vegan on meat/fish/dairy/egg are
--   left NULL too: no value would change any verdict, so none is invented.
--
-- Must apply after 014 (it references assumed_on_hand and migration:014
--   rows). Not applied anywhere by this change.

BEGIN;

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS contains_animal_byproduct BOOLEAN;

COMMENT ON COLUMN products.contains_animal_byproduct IS
    'Animal-derived but not meat, fish, dairy or egg: honey, beeswax, carmine/cochineal, shellac, bone-char-refined sugar, lanolin vitamin D3. Gelatin, lard, tallow and animal rennet are contains_meat instead. Drives vegan only. NULL = undecided and fails closed: such a product is withheld from vegans until reviewed.';

-- The 26 staples 014 inserted were all chosen by hand, so they get explicit
-- values here rather than NULL. All false except the two sugars.
--
-- Granulated Sugar, Brown Sugar -> TRUE. A generic "sugar" staple names no
--   source. Much North American cane sugar is decolourised with bone char;
--   beet sugar, organic and certified-vegan cane sugar are not, but the
--   staple does not say which it is. TRUE is the conservative value: a vegan
--   loses sugar as a free on-hand staple (a recipe can still cite a sugar
--   deal whose product is labelled false), whereas FALSE would be a guess
--   that serves bone-char sugar to vegans. A household that knows its sugar
--   is bone-char free can have a reviewer flip it.
UPDATE products
SET contains_animal_byproduct =
        lower(canonical_name) IN ('granulated sugar', 'brown sugar')
WHERE proposed_by = 'migration:014';

-- Honey: the staple 014 could not seed. Not meat/fish/dairy/egg/nuts, so it
-- clears vegetarian, pescatarian and nut-free; contains_animal_byproduct
-- keeps it from vegans. Same insert-or-flag-existing pattern as 014.
INSERT INTO products (
    canonical_name, category,
    contains_meat, contains_fish, contains_dairy, contains_egg, contains_nuts,
    contains_animal_byproduct,
    is_pantry_staple, assumed_on_hand,
    status, proposed_by, proposal_reason, reviewed_by, reviewed_at
)
SELECT 'Honey', 'pantry',
       false, false, false, false, false,
       true,
       true, true,
       'confirmed', 'migration:015', 'starter pantry staple',
       'migration:015', now()
WHERE NOT EXISTS (
    SELECT 1 FROM products p
    WHERE lower(p.canonical_name) = 'honey'
      AND p.status <> 'rejected'
);

-- A live "Honey" row that already existed keeps its own reviewed attributes
-- and is flagged on hand, as 014 did. Its contains_animal_byproduct is filled
-- only if NULL, and only towards TRUE -- the fail-closed direction, and true
-- of honey by definition -- so no human decision is overwritten.
UPDATE products
SET assumed_on_hand = true,
    contains_animal_byproduct = COALESCE(contains_animal_byproduct, true)
WHERE status <> 'rejected'
  AND lower(canonical_name) = 'honey'
  AND (proposed_by IS DISTINCT FROM 'migration:015');

COMMIT;

-- Rollback:
--   BEGIN;
--   DELETE FROM products
--   WHERE proposed_by = 'migration:015'
--     AND NOT EXISTS (SELECT 1 FROM deal_products dp WHERE dp.product_id = products.product_id)
--     AND NOT EXISTS (SELECT 1 FROM product_aliases a WHERE a.product_id = products.product_id);
--   ALTER TABLE products DROP COLUMN IF EXISTS contains_animal_byproduct;
--   COMMIT;
--
-- Roll back the CODE first: products.ATTRIBUTES names this column, so the app
-- fails every attribute read once it is dropped. Dropping the column loses
-- every by-product decision reviewers made after 015. A pre-existing "Honey"
-- row flagged on hand above stays flagged (that flag is 014's column, and the
-- rollback cannot tell it from a flag set by hand).
