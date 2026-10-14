# Migrations

`scripts/init_db.sql` is the **origin** schema and is never edited in place.
Every schema change after it is a numbered file in this directory. The DDL and
the running database drifted apart once already; this is the discipline that
prevents a repeat.

## Numbering

`NNN_short_snake_case_description.sql`, three digits, zero-padded, applied in
filename order:

```
001_add_products.sql
002_discount_percentage_generated.sql
003_shopping_list_recipes_junction.sql
```

Gaps are fine, reuse is not. If two branches both claim `004`, the second one
to merge renumbers. Never renumber a migration that has been applied anywhere.

## Rules

1. **One concern per migration.** `products` and its foreign keys belong
   together; `products` and a generated column do not.
2. **Idempotent where it is free.** `CREATE TABLE IF NOT EXISTS`,
   `ADD COLUMN IF NOT EXISTS`. Do not contort a migration to achieve it.
3. **Additive first, destructive later.** Add the new column and backfill in
   one migration; drop the old one in a separate, later migration, after the
   application stops reading it. `deals.product_name` and
   `price_snapshots.product_name` stay as the raw ingest string well past the
   point where `product_id` exists.
4. **Every migration carries a rollback**, as a comment block at the bottom
   when a real `DOWN` is impractical. Say explicitly when a change is not
   reversible without data loss.
5. **Header comment states the why**, in one or two sentences, and names the
   design-log entry it implements.

## Applying

`make migrate` in `../sql4e` applies unapplied files to the `grocery_study`
database on the study container (localhost:5433) and records them in
`schema_migrations`. It never touches Neon.

Promotion to Neon is a separate, deliberate step. Nothing in this repo does it
automatically.

## Acceptance bar

The regression oracle is the existing test suite. Every migration must leave it
green:

```
cd .. && .venv/bin/python -m pytest -q -m "not llm"
```

## Template

```sql
-- 00N_thing.sql
-- Why: one or two sentences. Design log: <entry title>.
-- Hernandez: <chapter / rule this implements>

BEGIN;

-- ... DDL ...

COMMIT;

-- Rollback:
--   BEGIN; ... COMMIT;
-- or: not reversible without data loss, because <reason>.
```
