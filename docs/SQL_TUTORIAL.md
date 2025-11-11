# SQL Tutorial: Grocery Optimizer Database

## Table of Contents
1. [Introduction](#introduction)
2. [Database Schema Overview](#database-schema-overview)
3. [Basic SQL Operations](#basic-sql-operations)
4. [Understanding JOINs](#understanding-joins)
5. [Filtering and Sorting Data](#filtering-and-sorting-data)
6. [Aggregations and Grouping](#aggregations-and-grouping)
7. [Advanced PostgreSQL Features](#advanced-postgresql-features)
8. [Performance Optimization](#performance-optimization)
9. [Practical Examples](#practical-examples)
10. [Exercises](#exercises)

---

## Introduction

This tutorial uses the **Grocery Optimizer** application database to teach SQL concepts from beginner to intermediate level. The application helps users find the best grocery deals in their area and generate budget-friendly recipes.

**What you'll learn:**
- How to query relational databases
- Different types of JOINs and when to use them
- Working with aggregations and statistics
- PostgreSQL-specific features (JSONB, arrays, partitioning)
- Writing efficient queries with indexes

**Prerequisites:**
- Basic understanding of what a database is
- Familiarity with tables, rows, and columns

---

## Database Schema Overview

### Core Tables

Our database contains 8 main tables:

#### 1. **users** - Customer Information
```sql
CREATE TABLE users (
    user_id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    postal_code VARCHAR(10),
    budget DECIMAL(10,2),
    household_size INTEGER DEFAULT 1,
    dietary_restrictions JSONB DEFAULT '[]',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

**Purpose:** Stores user account information, preferences, and budget constraints.

#### 2. **stores** - Grocery Store Locations
```sql
CREATE TABLE stores (
    store_id SERIAL PRIMARY KEY,
    name VARCHAR(255),
    chain VARCHAR(100),
    postal_code VARCHAR(10),
    address TEXT,
    latitude DECIMAL(10,8),
    longitude DECIMAL(11,8),
    last_updated TIMESTAMP DEFAULT NOW(),
    is_active BOOLEAN DEFAULT true
);
```

**Purpose:** Contains grocery store locations and details.

#### 3. **deals** - Current Sales and Discounts
```sql
CREATE TABLE deals (
    deal_id SERIAL PRIMARY KEY,
    store_id INTEGER REFERENCES stores(store_id) ON DELETE CASCADE,
    product_name VARCHAR(255),
    brand VARCHAR(100),
    sale_price DECIMAL(10,2),
    regular_price DECIMAL(10,2),
    discount_percentage INTEGER,
    unit VARCHAR(50),
    category VARCHAR(100),
    valid_from DATE,
    valid_until DATE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
```

**Purpose:** Tracks current deals available at stores. Each deal belongs to ONE store (many-to-one relationship).

#### 4. **recipes** - User-Generated Recipes
```sql
CREATE TABLE recipes (
    recipe_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    name VARCHAR(255),
    ingredients JSONB,
    instructions TEXT[],
    total_cost DECIMAL(10,2),
    servings INTEGER DEFAULT 4,
    prep_time INTEGER,
    cook_time INTEGER,
    cuisine_type VARCHAR(100),
    meal_type VARCHAR(50),
    nutritional_info JSONB,
    allergen_info JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**Purpose:** Stores recipes with flexible ingredient lists and nutritional data.

#### 5. **shopping_lists** - Generated Shopping Lists
```sql
CREATE TABLE shopping_lists (
    list_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE CASCADE,
    recipe_ids INTEGER[],
    items JSONB,
    total_cost DECIMAL(10,2),
    estimated_savings DECIMAL(10,2),
    regular_total DECIMAL(10,2),
    created_at TIMESTAMP DEFAULT NOW(),
    is_completed BOOLEAN DEFAULT false
);
```

**Purpose:** Shopping lists generated from recipes, optimized for best deals.

#### 6. **price_snapshots** - Price History (Partitioned)
```sql
CREATE TABLE price_snapshots (
    snapshot_id BIGSERIAL,
    time TIMESTAMPTZ NOT NULL,
    store_id INTEGER REFERENCES stores(store_id) ON DELETE CASCADE,
    product_name VARCHAR(255),
    brand VARCHAR(100),
    price DECIMAL(10,2),
    sale_price DECIMAL(10,2),
    unit VARCHAR(50),
    category VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW()
) PARTITION BY RANGE (time);
```

**Purpose:** Time-series data for price tracking and trend analysis. Uses table partitioning for performance.

#### 7. **api_usage** - Cost Tracking
```sql
CREATE TABLE api_usage (
    usage_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    model_name VARCHAR(100),
    tokens_used INTEGER,
    estimated_cost DECIMAL(10,4),
    endpoint VARCHAR(255),
    execution_time_ms INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**Purpose:** Tracks AI model usage and costs for analytics.

#### 8. **agent_logs** - Agent Execution Logs
```sql
CREATE TABLE agent_logs (
    log_id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(user_id) ON DELETE SET NULL,
    agent_name VARCHAR(100),
    task_type VARCHAR(100),
    input_data JSONB,
    output_data JSONB,
    execution_time_ms INTEGER,
    tokens_used INTEGER,
    status VARCHAR(50),
    error_message TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**Purpose:** Logs for debugging and monitoring AI agent operations.

### Entity Relationship Diagram (Text)

```
users (1) ----< (many) recipes
users (1) ----< (many) shopping_lists
users (1) ----< (many) api_usage
users (1) ----< (many) agent_logs

stores (1) ----< (many) deals
stores (1) ----< (many) price_snapshots
```

---

## Basic SQL Operations

### SELECT - Reading Data

The **SELECT** statement retrieves data from one or more tables.

#### Basic SELECT
```sql
-- Get all stores
SELECT * FROM stores;

-- Get specific columns
SELECT name, chain, postal_code FROM stores;

-- Get first 10 deals
SELECT product_name, sale_price, discount_percentage
FROM deals
LIMIT 10;
```

#### WHERE Clause - Filtering
```sql
-- Get stores in Toronto (postal code M5V)
SELECT name, address
FROM stores
WHERE postal_code = 'M5V1A1';

-- Get deals with 30% or more discount
SELECT product_name, discount_percentage, sale_price
FROM deals
WHERE discount_percentage >= 30;

-- Get active stores only
SELECT name, chain
FROM stores
WHERE is_active = true;
```

#### Multiple Conditions (AND, OR)
```sql
-- Deals with high discount AND in dairy category
SELECT product_name, sale_price, discount_percentage
FROM deals
WHERE discount_percentage >= 25
  AND category = 'Dairy';

-- Stores that are Loblaws OR Metro chain
SELECT name, chain, postal_code
FROM stores
WHERE chain = 'Loblaws' OR chain = 'Metro';

-- Using IN for multiple values
SELECT name, chain, postal_code
FROM stores
WHERE chain IN ('Loblaws', 'Metro', 'Sobeys');
```

### INSERT - Adding Data

```sql
-- Insert a new user
INSERT INTO users (email, postal_code, budget, household_size)
VALUES ('jane@example.com', 'M5V1A1', 200.00, 2);

-- Insert and return the new ID
INSERT INTO users (email, postal_code, budget, household_size)
VALUES ('john@example.com', 'H3B2A1', 150.00, 1)
RETURNING user_id, email;

-- Insert multiple rows
INSERT INTO stores (name, chain, postal_code, address)
VALUES
    ('Loblaws Downtown', 'Loblaws', 'M5V1A1', '123 Main St'),
    ('Metro Market', 'Metro', 'M5V1A1', '456 King St');
```

### UPDATE - Modifying Data

```sql
-- Update a user's budget
UPDATE users
SET budget = 250.00, updated_at = NOW()
WHERE user_id = 1;

-- Update multiple fields
UPDATE users
SET postal_code = 'M5H1A1',
    household_size = 3,
    updated_at = NOW()
WHERE email = 'jane@example.com';

-- Conditional update - deactivate old deals
UPDATE deals
SET updated_at = NOW()
WHERE valid_until < CURRENT_DATE;
```

### DELETE - Removing Data

```sql
-- Delete a specific deal
DELETE FROM deals
WHERE deal_id = 100;

-- Delete expired deals
DELETE FROM deals
WHERE valid_until < CURRENT_DATE - INTERVAL '30 days';

-- CAUTION: This deletes ALL rows!
DELETE FROM deals;  -- Don't run this!
```

**Note:** In this application, CASCADE deletes are used:
- Deleting a store will delete all its deals automatically
- Deleting a user will delete their shopping lists

---

## Understanding JOINs

**JOINs** combine rows from two or more tables based on related columns. This is one of the most powerful features of relational databases.

### Why Do We Need JOINs?

In our database, deals are stored in one table and stores in another. To get deal information WITH store details, we need to JOIN these tables.

**Without JOIN (incomplete information):**
```sql
SELECT product_name, sale_price, store_id
FROM deals
LIMIT 5;
```

Result:
```
product_name        | sale_price | store_id
--------------------|------------|----------
Organic Bananas     |       1.99 |        1
Milk 2% 2L          |       3.49 |        2
Chicken Breast      |       4.99 |        1
```

We only see `store_id = 1` or `2`, but what store is that?

### INNER JOIN

**INNER JOIN** returns rows where there's a match in BOTH tables.

```sql
SELECT
    d.product_name,
    d.sale_price,
    d.discount_percentage,
    s.name AS store_name,
    s.chain,
    s.postal_code
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
LIMIT 5;
```

Result:
```
product_name        | sale_price | discount_percentage | store_name           | chain    | postal_code
--------------------|------------|---------------------|----------------------|----------|------------
Organic Bananas     |       1.99 |                  25 | Loblaws City Market  | Loblaws  | M5V1A1
Milk 2% 2L          |       3.49 |                  30 | Metro Downtown       | Metro    | M5V1A1
Chicken Breast      |       4.99 |                  20 | Loblaws City Market  | Loblaws  | M5V1A1
```

**How it works:**
1. `FROM deals d` - Start with the deals table (give it alias `d`)
2. `INNER JOIN stores s` - Join with stores table (give it alias `s`)
3. `ON d.store_id = s.store_id` - Match rows where these columns are equal

**Visual Representation:**
```
deals table          stores table
-------------        -------------
deal_id | store_id   store_id | name
1       | 1          1        | Loblaws
2       | 2          2        | Metro
3       | 1          3        | Sobeys

Result: Rows where store_id matches
deal_id 1 + store 1 = "Organic Bananas at Loblaws"
deal_id 2 + store 2 = "Milk at Metro"
deal_id 3 + store 1 = "Chicken Breast at Loblaws"
```

### LEFT JOIN (LEFT OUTER JOIN)

**LEFT JOIN** returns ALL rows from the left table, and matching rows from the right table. If no match, NULL values are returned for right table columns.

```sql
-- Get all stores, even if they have no deals
SELECT
    s.name AS store_name,
    s.chain,
    COUNT(d.deal_id) AS num_deals
FROM stores s
LEFT JOIN deals d ON s.store_id = d.store_id
GROUP BY s.store_id, s.name, s.chain
ORDER BY num_deals DESC;
```

Result:
```
store_name           | chain    | num_deals
---------------------|----------|----------
Loblaws City Market  | Loblaws  |        15
Metro Downtown       | Metro    |        12
Sobeys West End      | Sobeys   |         8
IGA Express          | IGA      |         0  <-- No deals, but store still appears
```

**When to use LEFT JOIN:**
- When you want all records from the "main" table regardless of matches
- Example: "Show me all stores, and their deals if they have any"

### RIGHT JOIN (RIGHT OUTER JOIN)

**RIGHT JOIN** is the opposite of LEFT JOIN - returns all rows from the right table.

```sql
-- Get all deals, even if store information is missing (rare scenario)
SELECT
    d.product_name,
    d.sale_price,
    s.name AS store_name
FROM deals d
RIGHT JOIN stores s ON d.store_id = s.store_id;
```

**Note:** RIGHT JOIN is rarely used in practice. Most developers just flip the table order and use LEFT JOIN.

### FULL OUTER JOIN

**FULL OUTER JOIN** returns all rows from both tables, with NULLs where there's no match.

```sql
-- Get all stores AND all deals, even if they don't match (unusual for this schema)
SELECT
    s.name AS store_name,
    d.product_name
FROM stores s
FULL OUTER JOIN deals d ON s.store_id = d.store_id;
```

**When to use:** Rarely needed, but useful when you want everything from both tables regardless of relationships.

### CROSS JOIN

**CROSS JOIN** creates a Cartesian product - every row from the first table combined with every row from the second table.

```sql
-- Generate all possible combinations of stores and categories
SELECT
    s.name AS store_name,
    c.category
FROM stores s
CROSS JOIN (
    SELECT DISTINCT category FROM deals
) c
ORDER BY s.name, c.category;
```

**Warning:** If you have 100 stores and 20 categories, this returns 2,000 rows!

### Multiple JOINs

You can join more than two tables:

```sql
-- Get shopping lists with user info AND recipe details
SELECT
    u.email,
    u.budget,
    sl.list_id,
    sl.total_cost,
    sl.estimated_savings,
    r.name AS recipe_name
FROM shopping_lists sl
INNER JOIN users u ON sl.user_id = u.user_id
LEFT JOIN recipes r ON r.recipe_id = ANY(sl.recipe_ids)
WHERE u.postal_code = 'M5V1A1';
```

**Chain explanation:**
1. Start with shopping_lists
2. JOIN users to get user details
3. JOIN recipes to get recipe names (using array matching)

### Self JOIN

A table can be joined to itself:

```sql
-- Find stores from the same chain in different postal codes
SELECT
    s1.name AS store1,
    s1.postal_code AS postal1,
    s2.name AS store2,
    s2.postal_code AS postal2,
    s1.chain
FROM stores s1
INNER JOIN stores s2
    ON s1.chain = s2.chain
    AND s1.store_id < s2.store_id  -- Avoid duplicate pairs
WHERE s1.postal_code != s2.postal_code
ORDER BY s1.chain;
```

---

## Filtering and Sorting Data

### Date and Time Filtering

```sql
-- Get deals valid today
SELECT product_name, valid_from, valid_until
FROM deals
WHERE CURRENT_DATE BETWEEN valid_from AND valid_until;

-- Alternative syntax
SELECT product_name, valid_from, valid_until
FROM deals
WHERE valid_from <= CURRENT_DATE
  AND valid_until >= CURRENT_DATE;

-- Get deals expiring in the next 3 days
SELECT product_name, valid_until, discount_percentage
FROM deals
WHERE valid_until BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '3 days'
ORDER BY valid_until;

-- Get users created in the last 30 days
SELECT email, created_at
FROM users
WHERE created_at >= NOW() - INTERVAL '30 days';
```

### String Matching

```sql
-- Case-sensitive exact match
SELECT product_name FROM deals WHERE product_name = 'Milk 2% 2L';

-- Case-insensitive match (PostgreSQL)
SELECT product_name FROM deals WHERE product_name ILIKE 'milk%';

-- Pattern matching with LIKE
SELECT product_name FROM deals WHERE product_name LIKE '%Chicken%';

-- Patterns:
-- %     = any characters (zero or more)
-- _     = exactly one character
-- ILIKE = case-insensitive LIKE (PostgreSQL)

-- Find all organic products
SELECT product_name, sale_price
FROM deals
WHERE product_name ILIKE '%organic%';

-- Find products with specific brand patterns
SELECT product_name, brand
FROM deals
WHERE brand LIKE 'P%' OR brand LIKE 'N%'
ORDER BY brand;
```

### NULL Handling

```sql
-- Find users without a postal code
SELECT email, postal_code
FROM users
WHERE postal_code IS NULL;

-- Find users WITH a postal code
SELECT email, postal_code
FROM users
WHERE postal_code IS NOT NULL;

-- COALESCE - provide default value for NULL
SELECT
    email,
    COALESCE(postal_code, 'Not provided') AS postal_code,
    COALESCE(budget, 0.00) AS budget
FROM users;
```

### Sorting with ORDER BY

```sql
-- Sort deals by discount (highest first)
SELECT product_name, discount_percentage, sale_price
FROM deals
ORDER BY discount_percentage DESC;

-- Sort by multiple columns
SELECT product_name, category, discount_percentage
FROM deals
ORDER BY category ASC, discount_percentage DESC;

-- NULLS FIRST / NULLS LAST (PostgreSQL)
SELECT email, last_login
FROM users
ORDER BY last_login DESC NULLS LAST;
```

### LIMIT and OFFSET (Pagination)

```sql
-- Get top 10 deals
SELECT product_name, discount_percentage
FROM deals
ORDER BY discount_percentage DESC
LIMIT 10;

-- Pagination: Get results 11-20
SELECT product_name, discount_percentage
FROM deals
ORDER BY discount_percentage DESC
LIMIT 10 OFFSET 10;

-- Page 1: LIMIT 10 OFFSET 0
-- Page 2: LIMIT 10 OFFSET 10
-- Page 3: LIMIT 10 OFFSET 20
```

---

## Aggregations and Grouping

### Aggregate Functions

**Common aggregate functions:**
- `COUNT()` - Count rows
- `SUM()` - Total of values
- `AVG()` - Average value
- `MIN()` - Minimum value
- `MAX()` - Maximum value

```sql
-- Count total deals
SELECT COUNT(*) AS total_deals FROM deals;

-- Count deals by category
SELECT category, COUNT(*) AS num_deals
FROM deals
GROUP BY category
ORDER BY num_deals DESC;

-- Average discount percentage
SELECT AVG(discount_percentage) AS avg_discount
FROM deals;

-- Price statistics
SELECT
    MIN(sale_price) AS min_price,
    MAX(sale_price) AS max_price,
    AVG(sale_price) AS avg_price,
    SUM(sale_price) AS total_value
FROM deals;
```

### GROUP BY - Grouping Data

**GROUP BY** groups rows with the same values into summary rows.

```sql
-- Average discount by category
SELECT
    category,
    COUNT(*) AS num_deals,
    AVG(discount_percentage) AS avg_discount,
    AVG(sale_price) AS avg_sale_price
FROM deals
GROUP BY category
ORDER BY avg_discount DESC;
```

Result:
```
category        | num_deals | avg_discount | avg_sale_price
----------------|-----------|--------------|---------------
Meat & Seafood  |        12 |        28.33 |          12.45
Dairy           |        15 |        26.67 |           4.89
Produce         |        18 |        25.00 |           3.21
```

**Rules for GROUP BY:**
1. Every column in SELECT (except aggregate functions) must be in GROUP BY
2. Aggregate functions (COUNT, AVG, etc.) operate on each group

```sql
-- This is WRONG - postal_code not in GROUP BY
SELECT postal_code, category, COUNT(*)
FROM deals d
JOIN stores s ON d.store_id = s.store_id
GROUP BY category;  -- ERROR!

-- This is CORRECT
SELECT s.postal_code, d.category, COUNT(*) AS num_deals
FROM deals d
JOIN stores s ON d.store_id = s.store_id
GROUP BY s.postal_code, d.category
ORDER BY s.postal_code, num_deals DESC;
```

### HAVING - Filtering Groups

**WHERE** filters rows BEFORE grouping.
**HAVING** filters groups AFTER aggregation.

```sql
-- Get categories with more than 10 deals
SELECT
    category,
    COUNT(*) AS num_deals
FROM deals
GROUP BY category
HAVING COUNT(*) > 10
ORDER BY num_deals DESC;

-- Get stores with average discount > 25%
SELECT
    s.name AS store_name,
    COUNT(d.deal_id) AS num_deals,
    AVG(d.discount_percentage) AS avg_discount
FROM stores s
INNER JOIN deals d ON s.store_id = d.store_id
GROUP BY s.store_id, s.name
HAVING AVG(d.discount_percentage) > 25
ORDER BY avg_discount DESC;
```

### Example: Deal Statistics by Postal Code

This is from the actual application code:

```sql
SELECT
    COUNT(*) as total_deals,
    COUNT(DISTINCT d.category) as total_categories,
    COUNT(DISTINCT s.store_id) as total_stores,
    AVG(d.discount_percentage) as avg_discount,
    MAX(d.discount_percentage) as max_discount,
    MIN(d.sale_price) as min_price,
    MAX(d.sale_price) as max_price
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = 'M5V1A1'
  AND d.valid_until >= CURRENT_DATE
  AND s.is_active = true;
```

Result:
```
total_deals | total_categories | total_stores | avg_discount | max_discount | min_price | max_price
------------|------------------|--------------|--------------|--------------|-----------|----------
45          | 8                | 3            | 26.44        | 33           | 1.99      | 18.99
```

---

## Advanced PostgreSQL Features

### Working with JSONB

**JSONB** stores JSON data in a binary format, allowing efficient queries and indexing.

#### Querying JSONB

```sql
-- Get recipes with specific ingredient
SELECT name, ingredients
FROM recipes
WHERE ingredients @> '[{"name": "chicken"}]';

-- Extract JSONB field
SELECT
    name,
    ingredients->0->>'name' AS first_ingredient,
    ingredients->0->>'quantity' AS quantity
FROM recipes;

-- Count recipes by meal type (stored in JSONB)
SELECT
    meal_type,
    COUNT(*) AS num_recipes
FROM recipes
GROUP BY meal_type;

-- Get users with specific dietary restriction
SELECT email, dietary_restrictions
FROM users
WHERE dietary_restrictions @> '["vegetarian"]';
```

#### JSONB Operators

```
->    Get JSON object field (returns JSON)
->>   Get JSON object field (returns text)
@>    Contains (does left JSON contain right JSON)
<@    Is contained by
?     Does key exist
?&    Do all keys exist
?|    Do any keys exist
```

Examples:
```sql
-- Recipes with nutritional info containing "protein" key
SELECT name, nutritional_info
FROM recipes
WHERE nutritional_info ? 'protein';

-- Get specific nutritional value
SELECT
    name,
    nutritional_info->>'calories' AS calories,
    nutritional_info->>'protein' AS protein
FROM recipes
WHERE nutritional_info->>'calories' IS NOT NULL;
```

### Working with Arrays

PostgreSQL supports native array types.

```sql
-- Get shopping lists containing specific recipe
SELECT list_id, recipe_ids, total_cost
FROM shopping_lists
WHERE 5 = ANY(recipe_ids);

-- Get all recipes from a shopping list
SELECT
    sl.list_id,
    r.recipe_id,
    r.name AS recipe_name
FROM shopping_lists sl
INNER JOIN recipes r ON r.recipe_id = ANY(sl.recipe_ids)
WHERE sl.list_id = 1;

-- Count array elements
SELECT
    list_id,
    array_length(recipe_ids, 1) AS num_recipes,
    total_cost
FROM shopping_lists;

-- Unnest array to rows
SELECT
    list_id,
    unnest(recipe_ids) AS recipe_id
FROM shopping_lists
WHERE list_id = 1;
```

### Window Functions

**Window functions** perform calculations across rows related to the current row.

```sql
-- Rank deals by discount within each category
SELECT
    category,
    product_name,
    discount_percentage,
    RANK() OVER (PARTITION BY category ORDER BY discount_percentage DESC) AS rank_in_category
FROM deals
WHERE valid_until >= CURRENT_DATE
ORDER BY category, rank_in_category;
```

Result:
```
category  | product_name        | discount_percentage | rank_in_category
----------|---------------------|---------------------|------------------
Dairy     | Yogurt Greek 750g   |                  33 |                1
Dairy     | Milk 2% 2L          |                  30 |                2
Dairy     | Cheese Cheddar      |                  25 |                3
Meat      | Chicken Breast      |                  30 |                1
Meat      | Ground Beef         |                  25 |                2
```

```sql
-- Running total of deal savings by date
SELECT
    valid_from,
    product_name,
    (regular_price - sale_price) AS savings,
    SUM(regular_price - sale_price) OVER (ORDER BY valid_from) AS running_total_savings
FROM deals
ORDER BY valid_from;

-- Compare each deal to category average
SELECT
    category,
    product_name,
    discount_percentage,
    AVG(discount_percentage) OVER (PARTITION BY category) AS category_avg,
    discount_percentage - AVG(discount_percentage) OVER (PARTITION BY category) AS diff_from_avg
FROM deals
ORDER BY category, discount_percentage DESC;
```

**Common window functions:**
- `ROW_NUMBER()` - Sequential number
- `RANK()` - Rank with gaps for ties
- `DENSE_RANK()` - Rank without gaps
- `LEAD()` - Access next row
- `LAG()` - Access previous row
- `FIRST_VALUE()` - First value in window
- `LAST_VALUE()` - Last value in window

### Common Table Expressions (CTEs)

**CTEs** make complex queries more readable with temporary named result sets.

```sql
-- Find stores with above-average deal counts
WITH store_deal_counts AS (
    SELECT
        s.store_id,
        s.name,
        COUNT(d.deal_id) AS num_deals
    FROM stores s
    LEFT JOIN deals d ON s.store_id = d.store_id
    GROUP BY s.store_id, s.name
),
average_deals AS (
    SELECT AVG(num_deals) AS avg_deals
    FROM store_deal_counts
)
SELECT
    sdc.name,
    sdc.num_deals,
    ad.avg_deals,
    sdc.num_deals - ad.avg_deals AS diff_from_average
FROM store_deal_counts sdc
CROSS JOIN average_deals ad
WHERE sdc.num_deals > ad.avg_deals
ORDER BY sdc.num_deals DESC;
```

### Subqueries

**Subqueries** are queries nested inside other queries.

```sql
-- Get deals from stores with most deals
SELECT
    product_name,
    sale_price,
    discount_percentage
FROM deals
WHERE store_id = (
    SELECT store_id
    FROM deals
    WHERE valid_until >= CURRENT_DATE
    GROUP BY store_id
    ORDER BY COUNT(*) DESC
    LIMIT 1
);

-- Get users who spend more than average
SELECT email, budget
FROM users
WHERE budget > (SELECT AVG(budget) FROM users);

-- Get categories with deals in specific postal code
SELECT DISTINCT category
FROM deals
WHERE store_id IN (
    SELECT store_id
    FROM stores
    WHERE postal_code = 'M5V1A1'
);
```

### EXISTS vs IN

**EXISTS** is often faster for checking existence:

```sql
-- Find users who have created recipes (using EXISTS)
SELECT u.email, u.user_id
FROM users u
WHERE EXISTS (
    SELECT 1
    FROM recipes r
    WHERE r.user_id = u.user_id
);

-- Same query using IN
SELECT email, user_id
FROM users
WHERE user_id IN (
    SELECT DISTINCT user_id
    FROM recipes
    WHERE user_id IS NOT NULL
);
```

**When to use:**
- `EXISTS` - Generally faster for large datasets, stops at first match
- `IN` - Better for small subquery results

---

## Performance Optimization

### Understanding Indexes

**Indexes** speed up data retrieval but slow down inserts/updates.

#### B-Tree Indexes (Default)

```sql
-- Index on postal_code for fast lookups
CREATE INDEX idx_stores_postal_code ON stores(postal_code);

-- Now this query is fast:
SELECT * FROM stores WHERE postal_code = 'M5V1A1';

-- Composite index for multiple columns
CREATE INDEX idx_deals_valid_dates ON deals(valid_from, valid_until);

-- Helps with date range queries:
SELECT * FROM deals
WHERE valid_from <= CURRENT_DATE
  AND valid_until >= CURRENT_DATE;
```

#### Partial Indexes

Index only rows meeting a condition:

```sql
-- Only index active stores
CREATE INDEX idx_stores_is_active
ON stores(store_id)
WHERE is_active = true;

-- This query uses the smaller index:
SELECT * FROM stores WHERE is_active = true;
```

#### GIN Indexes for JSONB and Arrays

```sql
-- Index JSONB columns
CREATE INDEX idx_recipes_ingredients_gin
ON recipes USING GIN(ingredients);

-- Fast JSONB queries:
SELECT * FROM recipes
WHERE ingredients @> '[{"name": "chicken"}]';

-- Index for array operations
CREATE INDEX idx_shopping_lists_recipes_gin
ON shopping_lists USING GIN(recipe_ids);

-- Fast array queries:
SELECT * FROM shopping_lists WHERE 5 = ANY(recipe_ids);
```

#### Full-Text Search with GIN

```sql
-- Trigram index for fuzzy search
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX idx_price_snapshots_product_trgm
ON price_snapshots USING GIN(product_name gin_trgm_ops);

-- Fast fuzzy matching:
SELECT * FROM price_snapshots
WHERE product_name ILIKE '%chiken%';  -- Misspelled, but still finds results
```

### Query Performance Tips

#### Use EXPLAIN ANALYZE

```sql
-- See how PostgreSQL executes your query
EXPLAIN ANALYZE
SELECT d.product_name, s.name
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = 'M5V1A1';
```

Look for:
- **Seq Scan** (slow) vs **Index Scan** (fast)
- **Nested Loop** vs **Hash Join** vs **Merge Join**
- Actual execution time

#### Avoid SELECT *

```sql
-- BAD: Retrieves all columns
SELECT * FROM deals;

-- GOOD: Only get what you need
SELECT product_name, sale_price, discount_percentage FROM deals;
```

#### Use LIMIT for Large Results

```sql
-- Get top 10 deals without scanning all rows
SELECT product_name, discount_percentage
FROM deals
WHERE valid_until >= CURRENT_DATE
ORDER BY discount_percentage DESC
LIMIT 10;
```

#### Filter Early with WHERE

```sql
-- GOOD: Filter before JOIN
SELECT d.product_name, s.name
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = 'M5V1A1'
  AND d.valid_until >= CURRENT_DATE;

-- LESS EFFICIENT: Filter after JOIN
SELECT d.product_name, s.name
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = 'M5V1A1';
```

### Table Partitioning

**Partitioning** splits large tables into smaller pieces.

The `price_snapshots` table uses **range partitioning** by month:

```sql
CREATE TABLE price_snapshots (
    -- columns...
) PARTITION BY RANGE (time);

-- Create monthly partitions
CREATE TABLE price_snapshots_2025_10
PARTITION OF price_snapshots
FOR VALUES FROM ('2025-10-01') TO ('2025-11-01');

CREATE TABLE price_snapshots_2025_11
PARTITION OF price_snapshots
FOR VALUES FROM ('2025-11-01') TO ('2025-12-01');
```

**Benefits:**
- Queries only scan relevant partitions
- Can drop old partitions quickly
- Better for time-series data

```sql
-- Only scans November partition
SELECT * FROM price_snapshots
WHERE time >= '2025-11-01' AND time < '2025-12-01';
```

### Materialized Views

**Materialized views** store query results physically:

```sql
-- Create materialized view for expensive query
CREATE MATERIALIZED VIEW best_deals_by_category AS
SELECT
    d.category,
    d.product_name,
    d.sale_price,
    d.discount_percentage,
    s.name AS store_name
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE d.discount_percentage >= 20
  AND d.valid_until >= CURRENT_DATE
ORDER BY d.category, d.discount_percentage DESC;

CREATE INDEX idx_best_deals_category ON best_deals_by_category(category);

-- Query the view (fast!)
SELECT * FROM best_deals_by_category
WHERE category = 'Dairy';

-- Refresh when data changes
REFRESH MATERIALIZED VIEW best_deals_by_category;
```

---

## Practical Examples

### Example 1: Find Best Deals Near User

**Goal:** Get top 10 deals in user's postal code, sorted by discount.

```sql
SELECT
    d.product_name,
    d.brand,
    d.sale_price,
    d.regular_price,
    d.discount_percentage,
    d.category,
    s.name AS store_name,
    s.chain,
    s.address
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = 'M5V1A1'
  AND d.valid_until >= CURRENT_DATE
  AND d.valid_from <= CURRENT_DATE
  AND s.is_active = true
ORDER BY d.discount_percentage DESC, d.sale_price ASC
LIMIT 10;
```

**Breakdown:**
1. JOIN deals with stores to get store info
2. Filter by postal code
3. Filter by valid date range
4. Filter only active stores
5. Sort by discount (highest first), then price (lowest first)
6. Limit to top 10

### Example 2: Calculate Potential Savings

**Goal:** Show user how much they could save by shopping deals.

```sql
SELECT
    s.postal_code,
    COUNT(*) AS num_deals,
    SUM(d.regular_price - d.sale_price) AS total_savings,
    AVG(d.discount_percentage) AS avg_discount,
    MAX(d.regular_price - d.sale_price) AS best_single_saving
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = 'M5V1A1'
  AND d.valid_until >= CURRENT_DATE
GROUP BY s.postal_code;
```

Result:
```
postal_code | num_deals | total_savings | avg_discount | best_single_saving
------------|-----------|---------------|--------------|-------------------
M5V1A1      |        45 |        127.55 |        26.44 |              12.00
```

### Example 3: Search for Product Deals

**Goal:** Search for deals matching "chicken" in any postal code.

```sql
SELECT
    d.product_name,
    d.brand,
    d.sale_price,
    d.discount_percentage,
    s.name AS store_name,
    s.postal_code
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE (d.product_name ILIKE '%chicken%' OR d.brand ILIKE '%chicken%')
  AND d.valid_until >= CURRENT_DATE
  AND s.is_active = true
ORDER BY d.discount_percentage DESC
LIMIT 20;
```

**Key points:**
- `ILIKE` for case-insensitive search
- `%` wildcards for partial matching
- Search in both product_name and brand

### Example 4: User Recipe History

**Goal:** Get all recipes created by a user with ingredient details.

```sql
SELECT
    r.recipe_id,
    r.name,
    r.total_cost,
    r.servings,
    r.prep_time + r.cook_time AS total_time,
    jsonb_array_length(r.ingredients) AS ingredient_count,
    array_length(r.instructions, 1) AS step_count,
    r.created_at
FROM recipes r
WHERE r.user_id = 1
ORDER BY r.created_at DESC;
```

**Functions used:**
- `jsonb_array_length()` - Count items in JSONB array
- `array_length(array, dimension)` - Count items in PostgreSQL array

### Example 5: Price Trend Analysis

**Goal:** Compare current price to 30-day average.

```sql
WITH recent_prices AS (
    SELECT
        product_name,
        category,
        AVG(sale_price) AS avg_price_30d,
        MIN(sale_price) AS min_price_30d,
        MAX(sale_price) AS max_price_30d,
        COUNT(*) AS price_points
    FROM price_snapshots
    WHERE time >= NOW() - INTERVAL '30 days'
      AND store_id IN (SELECT store_id FROM stores WHERE postal_code = 'M5V1A1')
    GROUP BY product_name, category
)
SELECT
    d.product_name,
    d.sale_price AS current_price,
    rp.avg_price_30d,
    rp.min_price_30d,
    rp.max_price_30d,
    CASE
        WHEN d.sale_price < rp.avg_price_30d THEN 'Better than average'
        WHEN d.sale_price > rp.avg_price_30d THEN 'Worse than average'
        ELSE 'Average'
    END AS price_status
FROM deals d
INNER JOIN recent_prices rp ON d.product_name = rp.product_name
WHERE d.valid_until >= CURRENT_DATE
ORDER BY (rp.avg_price_30d - d.sale_price) DESC;
```

**Concepts used:**
- CTE (WITH clause) for readability
- Aggregate functions over time window
- CASE statement for conditional logic
- Subquery in WHERE clause

### Example 6: Store Performance Comparison

**Goal:** Compare stores by deal count and average discount.

```sql
SELECT
    s.chain,
    s.name,
    COUNT(d.deal_id) AS num_deals,
    AVG(d.discount_percentage) AS avg_discount,
    MIN(d.sale_price) AS cheapest_item,
    MAX(d.discount_percentage) AS best_discount,
    RANK() OVER (ORDER BY COUNT(d.deal_id) DESC) AS rank_by_deals,
    RANK() OVER (ORDER BY AVG(d.discount_percentage) DESC) AS rank_by_discount
FROM stores s
LEFT JOIN deals d ON s.store_id = d.store_id
    AND d.valid_until >= CURRENT_DATE
WHERE s.is_active = true
GROUP BY s.store_id, s.chain, s.name
HAVING COUNT(d.deal_id) > 0
ORDER BY avg_discount DESC;
```

**Features:**
- LEFT JOIN to include stores with no deals
- HAVING to filter out stores with 0 deals
- Window functions to rank stores
- Multiple aggregations

---

## Exercises

### Beginner Level

**Exercise 1:** Get all deals in the "Produce" category.
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
SELECT product_name, sale_price, discount_percentage
FROM deals
WHERE category = 'Produce'
  AND valid_until >= CURRENT_DATE
ORDER BY discount_percentage DESC;
```
</details>

**Exercise 2:** Count how many stores are in each chain.
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
SELECT
    chain,
    COUNT(*) AS num_stores
FROM stores
WHERE is_active = true
GROUP BY chain
ORDER BY num_stores DESC;
```
</details>

**Exercise 3:** Find users with a budget greater than $150.
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
SELECT email, budget, household_size
FROM users
WHERE budget > 150.00
ORDER BY budget DESC;
```
</details>

### Intermediate Level

**Exercise 4:** Get deals with store information for postal code "H3B2A1", only showing deals with at least 25% discount.
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
SELECT
    d.product_name,
    d.sale_price,
    d.regular_price,
    d.discount_percentage,
    s.name AS store_name,
    s.address
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = 'H3B2A1'
  AND d.discount_percentage >= 25
  AND d.valid_until >= CURRENT_DATE
ORDER BY d.discount_percentage DESC;
```
</details>

**Exercise 5:** Calculate the average number of deals per store, grouped by chain.
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
WITH store_deals AS (
    SELECT
        s.chain,
        s.store_id,
        COUNT(d.deal_id) AS num_deals
    FROM stores s
    LEFT JOIN deals d ON s.store_id = d.store_id
    WHERE s.is_active = true
    GROUP BY s.chain, s.store_id
)
SELECT
    chain,
    AVG(num_deals) AS avg_deals_per_store,
    SUM(num_deals) AS total_deals,
    COUNT(*) AS num_stores
FROM store_deals
GROUP BY chain
ORDER BY avg_deals_per_store DESC;
```
</details>

**Exercise 6:** Find all products that appear in multiple stores within postal code "M5V1A1".
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
SELECT
    d.product_name,
    COUNT(DISTINCT d.store_id) AS num_stores,
    MIN(d.sale_price) AS cheapest_price,
    MAX(d.sale_price) AS most_expensive_price,
    MAX(d.discount_percentage) AS best_discount
FROM deals d
INNER JOIN stores s ON d.store_id = s.store_id
WHERE s.postal_code = 'M5V1A1'
  AND d.valid_until >= CURRENT_DATE
GROUP BY d.product_name
HAVING COUNT(DISTINCT d.store_id) > 1
ORDER BY (MAX(d.sale_price) - MIN(d.sale_price)) DESC;
```
</details>

### Advanced Level

**Exercise 7:** Create a query that shows, for each category, the store with the most deals and the average discount.
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
WITH category_store_stats AS (
    SELECT
        d.category,
        s.name AS store_name,
        s.store_id,
        COUNT(d.deal_id) AS num_deals,
        AVG(d.discount_percentage) AS avg_discount
    FROM deals d
    INNER JOIN stores s ON d.store_id = s.store_id
    WHERE d.valid_until >= CURRENT_DATE
      AND s.is_active = true
    GROUP BY d.category, s.store_id, s.name
),
ranked_stores AS (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY category ORDER BY num_deals DESC) AS rn
    FROM category_store_stats
)
SELECT
    category,
    store_name,
    num_deals,
    ROUND(avg_discount, 2) AS avg_discount
FROM ranked_stores
WHERE rn = 1
ORDER BY num_deals DESC;
```
</details>

**Exercise 8:** Find "shopping buddies" - pairs of users with the same postal code and similar budgets (within $20).
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
SELECT
    u1.email AS user1,
    u2.email AS user2,
    u1.postal_code,
    u1.budget AS budget1,
    u2.budget AS budget2,
    ABS(u1.budget - u2.budget) AS budget_diff
FROM users u1
INNER JOIN users u2
    ON u1.postal_code = u2.postal_code
    AND u1.user_id < u2.user_id  -- Avoid duplicate pairs
WHERE ABS(u1.budget - u2.budget) <= 20
ORDER BY u1.postal_code, budget_diff;
```
</details>

**Exercise 9:** Create a report showing each postal code's "deal quality score" based on average discount and price.
```sql
-- Your query here
```

<details>
<summary>Solution</summary>

```sql
WITH postal_stats AS (
    SELECT
        s.postal_code,
        COUNT(d.deal_id) AS num_deals,
        AVG(d.discount_percentage) AS avg_discount,
        AVG(d.sale_price) AS avg_price,
        COUNT(DISTINCT d.category) AS num_categories,
        COUNT(DISTINCT s.store_id) AS num_stores
    FROM stores s
    LEFT JOIN deals d ON s.store_id = d.store_id
        AND d.valid_until >= CURRENT_DATE
    WHERE s.is_active = true
    GROUP BY s.postal_code
),
scores AS (
    SELECT
        *,
        -- Score: higher discount + more deals + more variety = better
        (avg_discount * 0.4) +
        (LEAST(num_deals, 50) * 0.3) +
        (num_categories * 2) +
        (num_stores * 3) AS quality_score
    FROM postal_stats
    WHERE num_deals > 0
)
SELECT
    postal_code,
    num_deals,
    ROUND(avg_discount, 1) AS avg_discount,
    ROUND(avg_price, 2) AS avg_price,
    num_categories,
    num_stores,
    ROUND(quality_score, 1) AS quality_score,
    RANK() OVER (ORDER BY quality_score DESC) AS rank
FROM scores
ORDER BY quality_score DESC;
```
</details>

---

## Additional Resources

### Key SQL Concepts Summary

| Concept | Purpose | Example |
|---------|---------|---------|
| **SELECT** | Retrieve data | `SELECT name FROM stores` |
| **WHERE** | Filter rows | `WHERE price < 10` |
| **JOIN** | Combine tables | `FROM deals JOIN stores ON ...` |
| **GROUP BY** | Aggregate data | `GROUP BY category` |
| **HAVING** | Filter groups | `HAVING COUNT(*) > 5` |
| **ORDER BY** | Sort results | `ORDER BY price DESC` |
| **LIMIT** | Limit results | `LIMIT 10` |
| **DISTINCT** | Remove duplicates | `SELECT DISTINCT category` |
| **UNION** | Combine queries | `query1 UNION query2` |
| **CTE (WITH)** | Temporary result set | `WITH temp AS (...)` |

### PostgreSQL-Specific Features

- **JSONB**: Efficient JSON storage with indexing
- **Arrays**: Native array types (INTEGER[], TEXT[])
- **ILIKE**: Case-insensitive LIKE
- **RETURNING**: Return data from INSERT/UPDATE/DELETE
- **Partitioning**: Split large tables
- **GIN Indexes**: For full-text and JSONB search
- **Window Functions**: Advanced analytics
- **pg_trgm**: Fuzzy text matching

### Best Practices

1. **Always use WHERE conditions** to limit result sets
2. **Index frequently queried columns** (postal_code, dates, foreign keys)
3. **Use JOINs instead of multiple queries** (more efficient)
4. **Select only needed columns** (avoid SELECT *)
5. **Use EXPLAIN ANALYZE** to understand query performance
6. **Create indexes on foreign keys** used in JOINs
7. **Use CTEs** for complex queries (readability)
8. **Leverage PostgreSQL features** (JSONB, arrays) when appropriate
9. **Test queries with LIMIT** before running on full dataset
10. **Use transactions** for data integrity

### Common Mistakes to Avoid

❌ **Forgetting WHERE clause** - Returns entire table
❌ **N+1 queries** - Use JOIN instead of loop
❌ **Not using indexes** - Slow queries on large tables
❌ **Using SELECT *** - Wastes bandwidth
❌ **Comparing NULL with =** - Use IS NULL
❌ **Not filtering early** - Filter before JOIN when possible
❌ **Missing GROUP BY columns** - All non-aggregated columns must be grouped
❌ **Ignoring query plans** - Use EXPLAIN to optimize

### Next Steps

1. **Practice with the sample data**: Run the seed scripts and experiment with queries
2. **Read the PostgreSQL documentation**: https://www.postgresql.org/docs/
3. **Explore the codebase**: See how queries are used in `/app/services/`
4. **Try the exercises**: Build progressively complex queries
5. **Experiment with EXPLAIN ANALYZE**: Understand query performance
6. **Learn about transactions**: ACID properties and isolation levels
7. **Study indexing strategies**: When and what to index

---

## Conclusion

This tutorial covered:
- ✅ Database schema and relationships
- ✅ Basic CRUD operations (SELECT, INSERT, UPDATE, DELETE)
- ✅ JOINs (INNER, LEFT, RIGHT, FULL, CROSS)
- ✅ Filtering with WHERE and HAVING
- ✅ Aggregations and GROUP BY
- ✅ Advanced PostgreSQL features (JSONB, arrays, partitioning)
- ✅ Performance optimization with indexes
- ✅ Real-world examples from the Grocery Optimizer app

**Key Takeaway:** SQL is a powerful language for working with relational data. The Grocery Optimizer application demonstrates real-world usage of JOINs, aggregations, and PostgreSQL-specific features to build an efficient, scalable system.

Keep practicing, and don't hesitate to experiment with different query approaches!
