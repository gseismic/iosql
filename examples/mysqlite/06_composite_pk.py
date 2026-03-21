import os
from iosql.utils.mysqlite import Database

DB_FILE = 'examples/data/composite_pk.sqlite3'

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE, auto_commit=True)

print("=== Composite Primary Key in insert_all ===\n")

print("1. Set pk directly in insert_all (no need to create table first):")
db['order_items'].insert_all([
    {'order_id': 1, 'product_id': 100, 'qty': 2},
    {'order_id': 1, 'product_id': 101, 'qty': 1},
    {'order_id': 2, 'product_id': 100, 'qty': 5},
], ensure_table=True, pk=['order_id', 'product_id'])

print(f"   pk: {db['order_items'].pk()}")
print(f"   rows: {db['order_items'].count()}")

print("\n2. Replace by composite key:")
db['order_items'].insert_all([
    {'order_id': 1, 'product_id': 100, 'qty': 99},
], ensure_table=True, pk=['order_id', 'product_id'], replace=True)

print("   After replacing qty=99 for order_id=1, product_id=100:")
for r in db['order_items'].select_all('*'):
    print(f"   {r}")

print("\n3. Same composite key with ignore:")
db['order_items'].insert_all([
    {'order_id': 1, 'product_id': 100, 'qty': 999},
], ensure_table=True, pk=['order_id', 'product_id'], ignore=True)

print("   After ignore (should still be qty=99):")
result = db['order_items'].select_one('*', where='order_id=1 AND product_id=100')
print(f"   qty = {result['qty']}")

print("\nDone!")
os.remove(DB_FILE)
