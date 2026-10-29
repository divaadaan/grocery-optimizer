-- 021_not_kosher.sql
-- Why: the frontend offered "kosher" but products.RESTRICTION_ATTRIBUTES had
--   no entry for it, so the restriction was unenforced -- and the Nutritionist
--   reported "Approved: all ingredients comply with the kosher restriction"
--   for recipes nothing had checked. This adds the missing per-product fact
--   as its own column, read by RESTRICTION_ATTRIBUTES for "kosher" only.
-- Design log: (new) "not_kosher: the kosher attribute".
-- Hernandez: single-field-value rule -- "is not kosher" is one fact about the
--   product.
--
-- Polarity: NOT kosher, so it reads like every contains_* attribute -- the
--   restriction requires it to be explicitly FALSE, TRUE is the restrictive
--   answer, and the gate's "IS NOT FALSE" / product_allows' explicit-False
--   test apply unchanged.
--
-- Labelling rule (also in products.ATTRIBUTE_RULES, which the proposer prompt
--   and both review surfaces show): FALSE only if the product is kosher,
--   certified or inherent -- it carries a recognised hechsher (OU, COR, MK,
--   Kof-K, Star-K, etc.) OR needs none (raw/fresh produce, eggs in shell,
--   plain dry grains and legumes, plain water, salt and similar).
--   Uncertified meat and poultry, uncertified cheese, pork, shellfish and
--   other forbidden species are TRUE. When the brand's certification is
--   uncertain, TRUE (least permissions). Decided from curated attributes
--   only, never from a name.
--
-- Meat-with-dairy is NOT this column: it is a recipe-level rule over the
--   cited products' contains_meat / contains_dairy
--   (products.RESTRICTION_COMBINATION_RULES, applied by agents/dietary.py).
--   No schema change is needed for it.
--
-- NULLABLE, like 015 and 017, with NO default: every existing product was
--   reviewed without this question being asked, so there is no honest value
--   to give it. NULL means "undecided" and FAILS CLOSED. Consequence: every
--   existing product is withheld from kosher users until a reviewer decides
--   it (`scripts/review_products.py --fill-undecided`, or the "undecided
--   attribute" card at /admin/review). This migration sets no values; the
--   labels need owner approval.
--
-- Audit plumbing: none needed. product_attribute_corrections (016) stores
--   the attribute name as text validated by the app against
--   products.ATTRIBUTES, with BOOLEAN old/new values and NULL old_value for
--   "was undecided" -- a not_kosher correction fits as-is. 018's merge
--   history and 019's link trigger enumerate no attributes.
--
-- Must apply after 017 (it follows that pattern). Not applied anywhere by
--   this change.

BEGIN;

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS not_kosher BOOLEAN;

COMMENT ON COLUMN products.not_kosher IS
    'Not kosher. FALSE only if kosher, certified or inherent: a recognised hechsher (OU, COR, MK, Kof-K, Star-K, etc.) or none needed (raw/fresh produce, eggs in shell, plain dry grains/legumes, plain water, salt and similar). Uncertified meat/poultry, uncertified cheese, pork, shellfish and other forbidden species are TRUE; uncertain certification is TRUE. Drives kosher only (meat-with-dairy is a recipe rule, not this column). NULL = undecided and fails closed: such a product is withheld from kosher users until reviewed.';

COMMIT;

-- Rollback:
--   BEGIN;
--   ALTER TABLE products DROP COLUMN IF EXISTS not_kosher;
--   COMMIT;
--
-- Roll back the CODE first: products.ATTRIBUTES names this column, so the app
-- fails every attribute read once it is dropped. Dropping the column loses
-- every kosher decision reviewers made after 021.
