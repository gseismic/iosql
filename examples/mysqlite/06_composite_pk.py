import os
from iosql.utils.mysqlite import Database

DB_FILE = 'examples/data/composite_pk.sqlite3'

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE, auto_commit=True)

print("=== Composite Primary Key (Multiple PKs) ===\n")

db['order_items'].create_table([
    {'name': 'order_id', 'type': 'INTEGER'},
    {'name': 'product_id', 'type': 'INTEGER'},
    {'name': 'quantity', 'type': 'INTEGER'},
    {'name': 'price', 'type': 'REAL'},
], pk=['order_id', 'product_id'])

db['order_items'].insert({'order_id': 1, 'product_id': 100, 'quantity': 2, 'price': 9.99})
db['order_items'].insert({'order_id': 1, 'product_id': 101, 'quantity': 1, 'price': 19.99})
db['order_items'].insert({'order_id': 2, 'product_id': 100, 'quantity': 5, 'price': 9.99})

print("Order items (order #1 has 2 products, order #2 has 1 product):")
for r in db['order_items'].select_all('*'):
    print(f"  Order {r['order_id']}, Product {r['product_id']}: qty={r['quantity']}, price={r['price']}")

print(f"\nTotal rows: {db['order_items'].count()}")

print("\nUsing replace to update quantity (same order_id + product_id):")
db['order_items'].insert({'order_id': 1, 'product_id': 100, 'quantity': 10, 'price': 9.99}, replace=True)
print("After replace (order 1, product 100 qty changed from 2 to 10):")
for r in db['order_items'].select_all('*'):
    print(f"  Order {r['order_id']}, Product {r['product_id']}: qty={r['quantity']}")

print("\nPrimary keys:", db['order_items'].pk())

print("\nDone!")
os.remove(DB_FILE)
