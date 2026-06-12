-- Sample Data for Grocery Optimizer Testing
-- Run this AFTER init_db.sql to populate with test data

-- ============================================================================
-- SAMPLE USERS
-- ============================================================================

INSERT INTO users (email, postal_code, budget, household_size, dietary_restrictions) VALUES
('alice@example.com', 'M5V3A8', 100.00, 2, '["vegetarian"]'::jsonb),
('bob@example.com', 'H2X1Y4', 150.00, 4, '["gluten_free", "no_nuts"]'::jsonb),
('charlie@example.com', 'V6B1A1', 75.00, 1, '[]'::jsonb),
('diana@example.com', 'M5V3A8', 120.00, 3, '["vegan"]'::jsonb),
('eve@example.com', 'H2X1Y4', 200.00, 5, '["kosher"]'::jsonb)
ON CONFLICT (email) DO NOTHING;

-- ============================================================================
-- SAMPLE STORES (Canadian Grocery Chains)
-- ============================================================================

INSERT INTO stores (name, chain, postal_code, address, city, province, latitude, longitude) VALUES
-- Toronto stores (M5V3A8 area)
('Loblaws King West', 'Loblaws', 'M5V3A8', '585 King St W', 'Toronto', 'ON', 43.6440, -79.4030),
('Metro Front Street', 'Metro', 'M5V3A8', '730 Bay St', 'Toronto', 'ON', 43.6600, -79.3850),
('No Frills Bathurst', 'No Frills', 'M5V3A8', '277 Bathurst St', 'Toronto', 'ON', 43.6500, -79.4050),
('Sobeys Wellesley', 'Sobeys', 'M5V3A8', '517 Yonge St', 'Toronto', 'ON', 43.6650, -79.3850),
-- Montreal stores (H2X1Y4 area)
('IGA Sainte-Catherine', 'IGA', 'H2X1Y4', '1430 Rue Sainte-Catherine E', 'Montreal', 'QC', 45.5160, -73.5600),
('Metro Berri', 'Metro', 'H2X1Y4', '1200 Rue Berri', 'Montreal', 'QC', 45.5150, -73.5650),
('Provigo Le Marché', 'Provigo', 'H2X1Y4', '1563 Boulevard René-Lévesque E', 'Montreal', 'QC', 45.5140, -73.5580),
-- Vancouver stores (V6B1A1 area)
('Safeway Davie', 'Safeway', 'V6B1A1', '1780 Davie St', 'Vancouver', 'BC', 49.2800, -123.1350),
('Save-On-Foods Granville', 'Save-On-Foods', 'V6B1A1', '888 Burrard St', 'Vancouver', 'BC', 49.2810, -123.1250),
('T&T Supermarket Metrotown', 'T&T Supermarket', 'V6B1A1', '4820 Kingsway', 'Vancouver', 'BC', 49.2260, -123.0030)
ON CONFLICT DO NOTHING;

-- ============================================================================
-- SAMPLE DEALS (Current Week)
-- ============================================================================

-- Get store IDs for reference
DO $$
DECLARE
    loblaws_id INTEGER;
    metro_to_id INTEGER;
    no_frills_id INTEGER;
    sobeys_id INTEGER;
    iga_id INTEGER;
    metro_mtl_id INTEGER;
    safeway_id INTEGER;
BEGIN
    -- Get store IDs
    SELECT store_id INTO loblaws_id FROM stores WHERE name = 'Loblaws King West' LIMIT 1;
    SELECT store_id INTO metro_to_id FROM stores WHERE name = 'Metro Front Street' LIMIT 1;
    SELECT store_id INTO no_frills_id FROM stores WHERE name = 'No Frills Bathurst' LIMIT 1;
    SELECT store_id INTO sobeys_id FROM stores WHERE name = 'Sobeys Wellesley' LIMIT 1;
    SELECT store_id INTO iga_id FROM stores WHERE name = 'IGA Sainte-Catherine' LIMIT 1;
    SELECT store_id INTO metro_mtl_id FROM stores WHERE name = 'Metro Berri' LIMIT 1;
    SELECT store_id INTO safeway_id FROM stores WHERE name = 'Safeway Davie' LIMIT 1;

    -- Loblaws deals (Toronto)
    INSERT INTO deals (store_id, product_name, brand, sale_price, regular_price, unit, category, valid_from, valid_until) VALUES
    (loblaws_id, 'Chicken Breast Boneless', 'PC', 8.99, 12.99, 'lb', 'Meat & Poultry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (loblaws_id, 'Broccoli Crowns', 'Fresh', 2.49, 3.99, 'lb', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (loblaws_id, 'Pasta Penne', 'PC Blue Menu', 1.49, 2.99, '454g', 'Pantry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (loblaws_id, 'Tomato Sauce', 'PC', 1.99, 3.49, '650ml', 'Pantry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (loblaws_id, 'Ground Beef Lean', 'PC', 5.99, 8.99, 'lb', 'Meat & Poultry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (loblaws_id, 'Eggs Large', 'PC Free Range', 4.99, 6.99, '12 count', 'Dairy & Eggs', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (loblaws_id, 'Milk 2%', 'PC', 4.49, 5.99, '2L', 'Dairy & Eggs', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (loblaws_id, 'Baby Spinach', 'Organics', 3.49, 4.99, '312g', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days');

    -- Metro deals (Toronto)
    INSERT INTO deals (store_id, product_name, brand, sale_price, regular_price, unit, category, valid_from, valid_until) VALUES
    (metro_to_id, 'Salmon Fillet Atlantic', 'Fresh', 9.99, 14.99, 'lb', 'Seafood', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (metro_to_id, 'Sweet Potatoes', 'Fresh', 1.99, 2.99, 'lb', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (metro_to_id, 'Rice Basmati', 'Tilda', 4.99, 7.99, '1kg', 'Pantry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (metro_to_id, 'Bell Peppers Red', 'Fresh', 1.49, 2.49, 'each', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (metro_to_id, 'Greek Yogurt', 'Oikos', 3.99, 5.99, '650g', 'Dairy & Eggs', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (metro_to_id, 'Cheese Cheddar', 'Black Diamond', 5.49, 7.99, '400g', 'Dairy & Eggs', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days');

    -- No Frills deals (Budget-focused)
    INSERT INTO deals (store_id, product_name, brand, sale_price, regular_price, unit, category, valid_from, valid_until) VALUES
    (no_frills_id, 'Bananas', 'Fresh', 0.69, 0.99, 'lb', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (no_frills_id, 'Carrots', 'Fresh', 1.49, 2.49, '2lb bag', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (no_frills_id, 'Potatoes Russet', 'Fresh', 2.99, 4.99, '5lb bag', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (no_frills_id, 'Onions Yellow', 'Fresh', 1.99, 2.99, '3lb bag', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (no_frills_id, 'Bread Whole Wheat', 'No Name', 1.99, 3.49, '675g', 'Bakery', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (no_frills_id, 'Pork Chops', 'Fresh', 3.99, 6.99, 'lb', 'Meat & Poultry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (no_frills_id, 'Canned Tomatoes', 'No Name', 0.99, 1.99, '796ml', 'Pantry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (no_frills_id, 'Spaghetti', 'No Name', 0.99, 1.99, '900g', 'Pantry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days');

    -- Sobeys deals (Toronto)
    INSERT INTO deals (store_id, product_name, brand, sale_price, regular_price, unit, category, valid_from, valid_until) VALUES
    (sobeys_id, 'Chicken Thighs Boneless', 'Fresh', 6.99, 9.99, 'lb', 'Meat & Poultry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (sobeys_id, 'Avocados', 'Fresh', 1.49, 2.49, 'each', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (sobeys_id, 'Quinoa', 'Compliments', 4.49, 6.99, '500g', 'Pantry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (sobeys_id, 'Olive Oil Extra Virgin', 'Compliments', 7.99, 11.99, '750ml', 'Pantry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (sobeys_id, 'Tofu Firm', 'Sunrise', 2.49, 3.99, '350g', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days');

    -- IGA deals (Montreal)
    INSERT INTO deals (store_id, product_name, brand, sale_price, regular_price, unit, category, valid_from, valid_until) VALUES
    (iga_id, 'Pork Tenderloin', 'Fresh', 7.99, 11.99, 'lb', 'Meat & Poultry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (iga_id, 'Zucchini', 'Fresh', 1.99, 2.99, 'lb', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (iga_id, 'Garlic', 'Fresh', 0.99, 1.99, 'bulb', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (iga_id, 'Butter Salted', 'Lactantia', 4.49, 6.49, '454g', 'Dairy & Eggs', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (iga_id, 'Cream Cheese', 'Philadelphia', 3.99, 5.49, '250g', 'Dairy & Eggs', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days');

    -- Metro deals (Montreal)
    INSERT INTO deals (store_id, product_name, brand, sale_price, regular_price, unit, category, valid_from, valid_until) VALUES
    (metro_mtl_id, 'Turkey Breast', 'Fresh', 5.99, 8.99, 'lb', 'Meat & Poultry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (metro_mtl_id, 'Mushrooms White', 'Fresh', 2.49, 3.99, '227g', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (metro_mtl_id, 'Lettuce Romaine', 'Fresh', 1.99, 2.99, 'each', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (metro_mtl_id, 'Tomatoes Cherry', 'Fresh', 2.99, 4.49, '1lb', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days');

    -- Safeway deals (Vancouver)
    INSERT INTO deals (store_id, product_name, brand, sale_price, regular_price, unit, category, valid_from, valid_until) VALUES
    (safeway_id, 'Beef Sirloin Steak', 'AAA', 9.99, 14.99, 'lb', 'Meat & Poultry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (safeway_id, 'Asparagus', 'Fresh', 2.99, 4.99, 'lb', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (safeway_id, 'Green Beans', 'Fresh', 1.99, 3.49, 'lb', 'Produce', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (safeway_id, 'Couscous', 'Near East', 2.99, 4.99, '200g', 'Pantry', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days'),
    (safeway_id, 'Sour Cream', 'Dairyland', 2.99, 4.49, '500ml', 'Dairy & Eggs', CURRENT_DATE, CURRENT_DATE + INTERVAL '6 days');

    RAISE NOTICE 'Sample deals inserted successfully';
END $$;

-- ============================================================================
-- SAMPLE PRICE SNAPSHOTS (Historical Data)
-- ============================================================================

-- Insert price history for the past 30 days for a few key products
INSERT INTO price_snapshots (time, store_id, product_name, brand, price, sale_price, unit, category)
SELECT
    NOW() - (generate_series || ' days')::INTERVAL as time,
    s.store_id,
    'Chicken Breast Boneless',
    'PC',
    12.99 + (random() * 2 - 1),  -- Price varies +/- $1
    CASE WHEN generate_series % 7 < 3 THEN 8.99 + (random() * 0.5) ELSE NULL END,  -- On sale ~3 days per week
    'lb',
    'Meat & Poultry'
FROM generate_series(0, 29) AS generate_series
CROSS JOIN (SELECT store_id FROM stores WHERE name = 'Loblaws King West' LIMIT 1) s;

INSERT INTO price_snapshots (time, store_id, product_name, brand, price, sale_price, unit, category)
SELECT
    NOW() - (generate_series || ' days')::INTERVAL as time,
    s.store_id,
    'Bananas',
    'Fresh',
    0.99,
    CASE WHEN generate_series % 7 < 2 THEN 0.69 ELSE NULL END,  -- On sale ~2 days per week
    'lb',
    'Produce'
FROM generate_series(0, 29) AS generate_series
CROSS JOIN (SELECT store_id FROM stores WHERE name = 'No Frills Bathurst' LIMIT 1) s;

-- ============================================================================
-- SAMPLE RECIPES
-- ============================================================================

INSERT INTO recipes (user_id, name, ingredients, instructions, total_cost, servings, prep_time, meal_type, cuisine_type, nutritional_info)
SELECT
    u.user_id,
    'Grilled Chicken with Broccoli',
    '[
        {"name": "Chicken Breast Boneless", "quantity": "1", "unit": "lb", "price": 8.99},
        {"name": "Broccoli Crowns", "quantity": "0.5", "unit": "lb", "price": 1.25},
        {"name": "Olive Oil", "quantity": "2", "unit": "tbsp", "price": 0.30},
        {"name": "Garlic", "quantity": "2", "unit": "cloves", "price": 0.20}
    ]'::jsonb,
    ARRAY['Preheat grill to medium-high heat', 'Season chicken with salt and pepper', 'Grill chicken for 6-7 minutes per side', 'Steam broccoli for 5 minutes', 'Sauté garlic in olive oil', 'Toss broccoli with garlic oil', 'Serve chicken with broccoli'],
    10.74,
    2,
    25,
    'dinner',
    'American',
    '{"calories_per_serving": 420, "protein_g": 52, "carbs_g": 12, "fat_g": 18, "fiber_g": 4, "vitamins": ["Vitamin C", "Vitamin K", "B6"]}'::jsonb
FROM users u WHERE u.email = 'alice@example.com' LIMIT 1;

INSERT INTO recipes (user_id, name, ingredients, instructions, total_cost, servings, prep_time, meal_type, cuisine_type, nutritional_info)
SELECT
    u.user_id,
    'Vegetarian Pasta Primavera',
    '[
        {"name": "Pasta Penne", "quantity": "1", "unit": "box", "price": 1.49},
        {"name": "Bell Peppers Red", "quantity": "2", "unit": "each", "price": 2.98},
        {"name": "Zucchini", "quantity": "1", "unit": "lb", "price": 1.99},
        {"name": "Tomato Sauce", "quantity": "1", "unit": "jar", "price": 1.99},
        {"name": "Garlic", "quantity": "3", "unit": "cloves", "price": 0.30}
    ]'::jsonb,
    ARRAY['Boil pasta according to package directions', 'Chop vegetables', 'Sauté garlic in olive oil', 'Add peppers and zucchini, cook 5 minutes', 'Add tomato sauce and simmer 10 minutes', 'Drain pasta and toss with vegetables', 'Serve hot with parmesan'],
    8.75,
    4,
    30,
    'dinner',
    'Italian',
    '{"calories_per_serving": 380, "protein_g": 12, "carbs_g": 68, "fat_g": 7, "fiber_g": 8, "vitamins": ["Vitamin C", "Vitamin A", "Iron"]}'::jsonb
FROM users u WHERE u.email = 'alice@example.com' LIMIT 1;

INSERT INTO recipes (user_id, name, ingredients, instructions, total_cost, servings, prep_time, meal_type, cuisine_type, nutritional_info)
SELECT
    u.user_id,
    'Budget-Friendly Pork Chops with Potatoes',
    '[
        {"name": "Pork Chops", "quantity": "1.5", "unit": "lb", "price": 5.99},
        {"name": "Potatoes Russet", "quantity": "2", "unit": "lbs", "price": 1.20},
        {"name": "Carrots", "quantity": "0.5", "unit": "lb", "price": 0.37},
        {"name": "Onions Yellow", "quantity": "1", "unit": "medium", "price": 0.33}
    ]'::jsonb,
    ARRAY['Preheat oven to 375°F', 'Season pork chops with salt and pepper', 'Sear pork chops in skillet 3 minutes per side', 'Cut potatoes and carrots into chunks', 'Place vegetables in baking dish', 'Top with pork chops', 'Bake 25-30 minutes until cooked through'],
    7.89,
    3,
    45,
    'dinner',
    'American',
    '{"calories_per_serving": 485, "protein_g": 38, "carbs_g": 42, "fat_g": 16, "fiber_g": 5, "vitamins": ["Vitamin A", "B12", "Iron"]}'::jsonb
FROM users u WHERE u.email = 'charlie@example.com' LIMIT 1;

-- ============================================================================
-- SAMPLE SHOPPING LISTS
-- ============================================================================

INSERT INTO shopping_lists (user_id, recipe_ids, items, total_cost, estimated_savings)
SELECT
    u.user_id,
    ARRAY[r1.recipe_id, r2.recipe_id],
    '[
        {"product": "Chicken Breast Boneless", "quantity": "1 lb", "store": "Loblaws King West", "price": 8.99},
        {"product": "Broccoli Crowns", "quantity": "0.5 lb", "store": "Loblaws King West", "price": 1.25},
        {"product": "Pasta Penne", "quantity": "1 box", "store": "Loblaws King West", "price": 1.49},
        {"product": "Bell Peppers Red", "quantity": "2 each", "store": "Metro Front Street", "price": 2.98},
        {"product": "Zucchini", "quantity": "1 lb", "store": "IGA Sainte-Catherine", "price": 1.99},
        {"product": "Tomato Sauce", "quantity": "1 jar", "store": "Loblaws King West", "price": 1.99},
        {"product": "Garlic", "quantity": "1 bulb", "store": "IGA Sainte-Catherine", "price": 0.99}
    ]'::jsonb,
    19.68,
    5.42
FROM users u
CROSS JOIN LATERAL (
    SELECT recipe_id FROM recipes WHERE user_id = u.user_id LIMIT 1
) r1
CROSS JOIN LATERAL (
    SELECT recipe_id FROM recipes WHERE user_id = u.user_id OFFSET 1 LIMIT 1
) r2
WHERE u.email = 'alice@example.com';

-- ============================================================================
-- SAMPLE API USAGE TRACKING
-- ============================================================================

INSERT INTO api_usage (user_id, model_name, tokens_used, estimated_cost, endpoint, execution_time_ms)
SELECT
    u.user_id,
    CASE (random() * 3)::int
        WHEN 0 THEN 'smollm:1.7b'
        WHEN 1 THEN 'smollm:360m'
        ELSE 'smollm:135m'
    END,
    (random() * 500 + 100)::int,
    (random() * 0.01 + 0.001)::decimal(10,4),
    '/api/v1/recipes/generate',
    (random() * 2000 + 500)::int
FROM generate_series(1, 50)
CROSS JOIN users u
WHERE random() > 0.5
LIMIT 50;

-- ============================================================================
-- VERIFICATION QUERIES
-- ============================================================================

DO $$
DECLARE
    user_count INTEGER;
    store_count INTEGER;
    deal_count INTEGER;
    recipe_count INTEGER;
    price_snapshot_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO user_count FROM users;
    SELECT COUNT(*) INTO store_count FROM stores;
    SELECT COUNT(*) INTO deal_count FROM deals;
    SELECT COUNT(*) INTO recipe_count FROM recipes;
    SELECT COUNT(*) INTO price_snapshot_count FROM price_snapshots;

    RAISE NOTICE '========================================';
    RAISE NOTICE 'SAMPLE DATA LOADED SUCCESSFULLY';
    RAISE NOTICE '========================================';
    RAISE NOTICE 'Users: %', user_count;
    RAISE NOTICE 'Stores: %', store_count;
    RAISE NOTICE 'Active Deals: %', deal_count;
    RAISE NOTICE 'Recipes: %', recipe_count;
    RAISE NOTICE 'Price Snapshots: %', price_snapshot_count;
    RAISE NOTICE '========================================';
END $$;

-- Display sample active deals
SELECT
    d.product_name,
    d.sale_price,
    d.regular_price,
    d.discount_percentage,
    s.chain
FROM deals d
JOIN stores s ON d.store_id = s.store_id
WHERE d.valid_until >= CURRENT_DATE
ORDER BY d.discount_percentage DESC
LIMIT 10;
