-- 017_contains_gluten.sql
-- Why: the frontend offers "gluten_free" but products.RESTRICTION_ATTRIBUTES
--   had no entry for it, so the restriction was unenforced (the app only
--   logged a warning). This adds the missing fact as its own column, read by
--   RESTRICTION_ATTRIBUTES for "gluten_free" only.
-- Design log: (new) "contains_gluten: the gluten_free attribute".
-- Hernandez: single-field-value rule -- "contains gluten" is one fact about
--   the product.
--
-- Labelling rule (also in products.ATTRIBUTE_RULES, the proposer prompt and
--   both review surfaces): contains_gluten covers wheat (including spelt,
--   kamut, durum, semolina, farro), barley, rye, triticale, malt and
--   brewer's yeast. Oats count as TRUE unless the product is labelled
--   certified gluten-free (cross-contamination). "May contain gluten" counts
--   as TRUE. Judge the product as typically sold in Canadian flyers. It is
--   decided from curated product attributes only, never from a name.
--
-- NULLABLE, like contains_animal_byproduct (015): every product confirmed
--   before this migration was reviewed without this question being asked, so
--   there is no honest value to give it. NULL means "undecided" and FAILS
--   CLOSED (the gate tests "IS NOT FALSE", product_allows requires an
--   explicit False). Consequence: existing products are withheld from
--   gluten_free users until a reviewer decides them --
--   `scripts/review_products.py --fill-undecided` or the "undecided
--   attribute" card at /admin/review.
--
-- Unlike 015, this migration sets NO values: not even for the pantry staples
--   014 hand-inserted. Labelling decisions need owner approval, so every
--   existing row stays NULL. The proposed values for the 014 staples are in
--   the PR description as a draft for the owner to approve.
--
-- Must apply after 015 (it follows that pattern). Not applied anywhere by
--   this change.

BEGIN;

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS contains_gluten BOOLEAN;

COMMENT ON COLUMN products.contains_gluten IS
    'Contains gluten: wheat (incl. spelt, kamut, durum, semolina, farro), barley, rye, triticale, malt, brewer''s yeast. Oats count as true unless labelled certified gluten-free; "may contain gluten" counts as true. Judged as typically sold in Canadian flyers. Drives gluten_free only. NULL = undecided and fails closed: such a product is withheld from gluten_free users until reviewed.';

COMMIT;

-- Rollback:
--   BEGIN;
--   ALTER TABLE products DROP COLUMN IF EXISTS contains_gluten;
--   COMMIT;
--
-- Roll back the CODE first: products.ATTRIBUTES names this column, so the app
-- fails every attribute read once it is dropped. Dropping the column loses
-- every gluten decision reviewers made after 017.
