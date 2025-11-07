"""
Database Setup Script - Native PostgreSQL
Run: python scripts/run_db_setup.py [--seed]
"""
import os
import sys
import argparse
from datetime import datetime, timedelta
import psycopg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not found in .env")
    sys.exit(1)

def run_schema(cursor):
    """Execute schema SQL file"""
    print("📄 Running schema...")
    schema_path = os.path.join(os.path.dirname(__file__), 'init_db.sql')
    with open(schema_path, 'r') as f:
        cursor.execute(f.read())
    print("✅ Schema created")

def seed_data(cursor):
    """Add sample stores and deals"""
    print("🌱 Seeding data...")
    
    # Stores
    stores = [
        ('Loblaws', 'Loblaws', 'M5V3A8', '585 Queens Quay W, Toronto', 43.6387, -79.3817),
        ('Metro', 'Metro', 'M5V3A8', '700 Bay St, Toronto', 43.6618, -79.3817),
        ('IGA', 'Sobeys', 'H2X1Y4', '1451 Rue Sainte-Catherine E, Montreal', 45.5189, -73.5640),
        ('Save-On-Foods', 'Save-On-Foods', 'V6B1A1', '1070 Davie St, Vancouver', 49.2788, -123.1284),
    ]
    
    store_ids = []
    for store in stores:
        cursor.execute("""
            INSERT INTO stores (name, chain, postal_code, address, latitude, longitude)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING store_id
        """, store)
        store_ids.append(cursor.fetchone()[0])
    
    # Deals
    today = datetime.now().date()
    week = today + timedelta(days=7)
    
    products = [
        ('Chicken Breast', 'Fresh', 7.99, 11.99, 'lb', 'Meat'),
        ('Ground Beef', 'Fresh', 5.99, 8.99, 'lb', 'Meat'),
        ('Eggs Large', 'White', 3.99, 5.99, 'dozen', 'Dairy'),
        ('Milk 2%', 'Lactantia', 4.49, 5.99, '2L', 'Dairy'),
        ('Bananas', 'Fresh', 0.69, 0.99, 'lb', 'Produce'),
    ]
    
    for store_id in store_ids:
        for prod in products:
            discount = int(((prod[3] - prod[2]) / prod[3]) * 100)
            cursor.execute("""
                INSERT INTO deals 
                (store_id, product_name, brand, sale_price, regular_price, 
                 discount_percentage, unit, category, valid_from, valid_until)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (store_id, prod[0], prod[1], prod[2], prod[3], discount, prod[4], prod[5], today, week))
    
    print(f"✅ Seeded {len(store_ids)} stores and {len(store_ids) * len(products)} deals")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', action='store_true', help='Add sample data')
    args = parser.parse_args()
    
    try:
        # psycopg3 uses different connection context
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cursor:
                run_schema(cursor)
                
                if args.seed:
                    seed_data(cursor)
                
                conn.commit()
                
                # Verify
                cursor.execute("SELECT COUNT(*) FROM stores")
                print(f"\n✅ Setup complete - {cursor.fetchone()[0]} stores")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()