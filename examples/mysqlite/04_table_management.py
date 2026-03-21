import os
from iosql.utils.mysqlite import Database

DB_FILE = 'examples/data/table_demo.sqlite3'

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE, auto_commit=True)

db['users'].create_table([
    {'name': 'id', 'type': 'INTEGER PRIMARY KEY'},
    {'name': 'name', 'type': 'TEXT'},
    {'name': 'email', 'type': 'TEXT'},
    {'name': 'age', 'type': 'INTEGER'},
])

print(f"Table exists: {db['users'].table_exists()}")

db['users'].insert({'id': 1, 'name': 'Alice', 'email': 'alice@example.com', 'age': 30})
db['users'].insert({'id': 2, 'name': 'Bob', 'email': 'bob@example.com', 'age': 25})

print(f"\nColumn info: {db['users'].column_info()}")
print(f"Default values: {db['users'].default_values()}")
print(f"Primary keys: {db['users'].pk()}")

db['users'].ensure_column('phone', 'TEXT')
print(f"\nAfter adding 'phone' column: {db['users'].column_info()}")

db['users'].create_index('name')
print(f"\nIndexes: {db['users'].get_all_index()}")

print(f"\nTables in database:")
for row in db['users'].query_all("SELECT name FROM sqlite_master WHERE type='table'"):
    print(f"  {row['name']}")

db['users'].drop_table()
print("\nAfter drop_table:")
print(f"  Table exists: {db['users'].table_exists()}")

print("\nDone!")
