-- 014_pantry_staples_assumed_on_hand.sql
-- Why: recipe ingredients now cite what they are made from -- a deal_id, or
--   the product_id of a staple the household is assumed to have -- and
--   dietary compliance is a lookup of that product's curated attributes
--   (app/agents/dietary.py). Staples were a hard-coded four-word list
--   (salt, pepper, oil, water) matched by NAME in costing.py, which nothing
--   could check for compliance. This makes "always on hand" a fact about a
--   confirmed product, and seeds a reviewed starter set so a SousChef can cite
--   garlic or soy sauce and the verdict reads contains_* rather than a name.
-- Design log: (new) "recipes cite products: staples become catalog rows".
-- Hernandez: single-field-value rule -- "always on hand" is one fact about
--   the product, distinct from is_pantry_staple ("shelf-stable basic bought by
--   the bag/box", set by the proposer: rice is one, and it is NOT free).
--
-- Not applied anywhere by this change. Until it is, fetch_pantry_staples()
-- fails on the missing column, the graph degrades to "no staples", and
-- SousChefs may cite assigned deals only.

BEGIN;

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS assumed_on_hand BOOLEAN NOT NULL DEFAULT false;

COMMENT ON COLUMN products.assumed_on_hand IS
    'The household is assumed to already have this, so a recipe may cite it by product_id without a deal and it costs nothing in the plan. Read only for status=''confirmed'' rows. Distinct from is_pantry_staple, which describes shelf-stable basics that still have to be bought.';

CREATE INDEX IF NOT EXISTS idx_products_assumed_on_hand
    ON products (product_id)
    WHERE assumed_on_hand AND status = 'confirmed';

-- Starter staples. Attributes are explicit and conservative; each was chosen
-- by hand and is listed for owner review in the change report. Notes:
--   * Butter: dairy.
--   * Fish sauce: fish. Worcestershire: fish (anchovies are a standard
--     ingredient).
--   * Soy sauce, Dijon: no tracked attribute (wheat/soy/mustard are allergens
--     the schema has no column for).
--   * Granulated / brown sugar: no attribute. Some cane sugar is refined with
--     bone char; the schema cannot express that, and it is not marked.
--   * Honey is deliberately NOT seeded: it is animal-derived and many vegans
--     avoid it, but it is not meat, fish, dairy, egg or nuts, so any value in
--     these columns would either let it through for vegans or lie about it.
--     It needs an attribute this schema does not have.
--   * Stocks/broths, mayonnaise, peanut butter, sesame oil are not seeded:
--     either not universally on hand, or they carry an attribute or allergen
--     that should be a deliberate per-household choice.
--
-- A name already live in products (the unique index ignores rejected rows)
-- is not inserted again. Such a row is flagged assumed_on_hand below instead,
-- keeping its own human-reviewed attributes; it is served only once confirmed.
INSERT INTO products (
    canonical_name, category,
    contains_meat, contains_fish, contains_dairy, contains_egg, contains_nuts,
    is_pantry_staple, assumed_on_hand,
    status, proposed_by, proposal_reason, reviewed_by, reviewed_at
)
SELECT v.canonical_name, v.category,
       v.meat, v.fish, v.dairy, v.egg, v.nuts,
       v.shelf_stable, true,
       'confirmed', 'migration:014', 'starter pantry staple',
       'migration:014', now()
FROM (VALUES
    -- canonical_name,        category,     meat,  fish,  dairy, egg,   nuts,  shelf_stable
    ('Salt',                  'pantry',     false, false, false, false, false, true),
    ('Black Pepper',          'spices',     false, false, false, false, false, true),
    ('Water',                 'pantry',     false, false, false, false, false, true),
    ('Vegetable Oil',         'pantry',     false, false, false, false, false, true),
    ('Olive Oil',             'pantry',     false, false, false, false, false, true),
    ('Butter',                'dairy',      false, false, true,  false, false, false),
    ('Garlic',                'produce',    false, false, false, false, false, false),
    ('Yellow Onion',          'produce',    false, false, false, false, false, false),
    ('All-Purpose Flour',     'baking',     false, false, false, false, false, true),
    ('Granulated Sugar',      'baking',     false, false, false, false, false, true),
    ('Brown Sugar',           'baking',     false, false, false, false, false, true),
    ('Baking Soda',           'baking',     false, false, false, false, false, true),
    ('Baking Powder',         'baking',     false, false, false, false, false, true),
    ('Cornstarch',            'baking',     false, false, false, false, false, true),
    ('Soy Sauce',             'condiments', false, false, false, false, false, true),
    ('Fish Sauce',            'condiments', false, true,  false, false, false, true),
    ('Worcestershire Sauce',  'condiments', false, true,  false, false, false, true),
    ('White Vinegar',         'condiments', false, false, false, false, false, true),
    ('Dijon Mustard',         'condiments', false, false, false, false, false, true),
    ('Garlic Powder',         'spices',     false, false, false, false, false, true),
    ('Ground Cumin',          'spices',     false, false, false, false, false, true),
    ('Paprika',               'spices',     false, false, false, false, false, true),
    ('Chili Flakes',          'spices',     false, false, false, false, false, true),
    ('Dried Oregano',         'spices',     false, false, false, false, false, true),
    ('Dried Basil',           'spices',     false, false, false, false, false, true),
    ('Ground Cinnamon',       'spices',     false, false, false, false, false, true)
) AS v(canonical_name, category, meat, fish, dairy, egg, nuts, shelf_stable)
WHERE NOT EXISTS (
    SELECT 1 FROM products p
    WHERE lower(p.canonical_name) = lower(v.canonical_name)
      AND p.status <> 'rejected'
);

UPDATE products
SET assumed_on_hand = true
WHERE status <> 'rejected'
  AND NOT assumed_on_hand
  AND lower(canonical_name) IN (
      'salt', 'black pepper', 'water', 'vegetable oil', 'olive oil', 'butter',
      'garlic', 'yellow onion', 'all-purpose flour', 'granulated sugar',
      'brown sugar', 'baking soda', 'baking powder', 'cornstarch', 'soy sauce',
      'fish sauce', 'worcestershire sauce', 'white vinegar', 'dijon mustard',
      'garlic powder', 'ground cumin', 'paprika', 'chili flakes',
      'dried oregano', 'dried basil', 'ground cinnamon'
  );

COMMIT;

-- Rollback:
--   BEGIN;
--   DELETE FROM products
--   WHERE proposed_by = 'migration:014'
--     AND NOT EXISTS (SELECT 1 FROM deal_products dp WHERE dp.product_id = products.product_id)
--     AND NOT EXISTS (SELECT 1 FROM product_aliases a WHERE a.product_id = products.product_id);
--   DROP INDEX IF EXISTS idx_products_assumed_on_hand;
--   ALTER TABLE products DROP COLUMN IF EXISTS assumed_on_hand;
--   COMMIT;
--
-- Reversible while nothing references the seeded rows. Once a deal or alias is
-- linked to one of them (e.g. a flyer "Garlic" deal), the guarded DELETE keeps
-- it, and dropping the column loses which pre-existing rows were flagged on
-- hand by the UPDATE above.
