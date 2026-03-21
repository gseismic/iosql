import os
from iosql.utils.mysqlite import Database

DB_FILE = 'examples/data/schemaless_demo.sqlite3'

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE, auto_commit=True)

print("=== Schemaless Mode: No need to create table first ===\n")

print("1. Direct insert - table auto-created:")
db['clicks'].insert({'user_id': 1, 'page': '/home', 'duration': 1.5}, ensure_table=True)
db['clicks'].insert({'user_id': 2, 'page': '/products', 'duration': 2.0})
print(f"   Table exists: {db['clicks'].table_exists()}")
print(f"   Count: {db['clicks'].count()}")
for row in db['clicks'].select_all('*'):
    print(f"   {row}")

print("\n2. Dynamic columns - add as you go:")
db['events'].insert({'id': 1, 'type': 'click'}, ensure_table=True)
db['events'].insert({'id': 2, 'type': 'scroll', 'delta_y': 300}, ensure_column=True)
db['events'].insert({'id': 3, 'type': 'keypress', 'key': 'Enter', 'modifiers': 'ctrl'}, ensure_column=True)
print(f"   Columns: {list(db['events'].column_info()['column_type'].keys())}")
for row in db['events'].select_all('*'):
    print(f"   {row}")

print("\n3. Batch insert - all auto-created:")
db['logs'].insert_all([
    {'level': 'INFO', 'msg': 'Started'},
    {'level': 'WARN', 'msg': 'Slow query'},
    {'level': 'ERROR', 'msg': 'Connection failed', 'retry': 3},
], ensure_table=True)
print(f"   Count: {db['logs'].count()}")
for row in db['logs'].select_all('*'):
    print(f"   {row}")

print("\n4. Replace existing record (needs pk):")
db['cache'].insert({'key': 'user:1', 'value': 'Alice'}, pk='key', ensure_table=True)
db['cache'].insert({'key': 'user:1', 'value': 'Alice Updated'}, replace=True)
print(f"   After replace (should be 1 row): {db['cache'].count()}")
print(f"   Value: {db['cache'].select_one('*')['value']}")

print("\n5. Ignore duplicate (needs pk):")
db['seen'].insert({'event_id': 100, 'ts': 123456}, pk='event_id', ensure_table=True)
db['seen'].insert({'event_id': 100, 'ts': 999999}, ignore=True)
result = db['seen'].select_all('*')
print(f"   Count (should be 1): {len(result)}")
print(f"   ts value (should be 123456): {result[0]['ts']}")

print("\nDone!")
os.remove(DB_FILE)
