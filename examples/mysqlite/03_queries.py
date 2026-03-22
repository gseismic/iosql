import os
from iosql.utils.mysqlite import Database

DB_FILE = 'examples/runtime/mysqlite/query_demo.sqlite3'
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)


if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE, auto_commit=True)

db['products'].create_table([
    {'name': 'id', 'type': 'INTEGER PRIMARY KEY'},
    {'name': 'name', 'type': 'TEXT'},
    {'name': 'category', 'type': 'TEXT'},
    {'name': 'price', 'type': 'REAL'},
    {'name': 'stock', 'type': 'INTEGER'},
])

products = [
    {'name': 'Apple', 'category': 'fruit', 'price': 1.5, 'stock': 100},
    {'name': 'Banana', 'category': 'fruit', 'price': 0.5, 'stock': 200},
    {'name': 'Carrot', 'category': 'vegetable', 'price': 1.2, 'stock': 50},
    {'name': 'Lettuce', 'category': 'vegetable', 'price': 2.0, 'stock': 30},
    {'name': 'Milk', 'category': 'dairy', 'price': 3.5, 'stock': 40},
]
db['products'].insert_all(products, ensure_table=True)

print("All products:")
for p in db['products'].select_all('*'):
    print(f"  {p}")

print("\nOnly names (single column):")
for row in db['products'].select_all('name'):
    print(f"  {row}")

print("\nSpecific columns:")
for row in db['products'].select_all(['name', 'price']):
    print(f"  {row}")

print("\nProducts in 'fruit' category:")
for p in db['products'].select_all('*', where="category='fruit'"):
    print(f"  {p}")

print("\nProducts with price > 1.0:")
for p in db['products'].select_all('*', where='price > 1.0'):
    print(f"  {p}")

print("\nUsing raw SQL for aggregation:")
for row in db['products'].query_all("SELECT category, SUM(stock) as total_stock FROM products GROUP BY category"):
    print(f"  {row}")

print("\nDone!")
