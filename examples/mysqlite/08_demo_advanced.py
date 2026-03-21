import os
from iosql.utils.mysqlite import Database

DB_FILE = 'examples/data/demo_advanced.sqlite3'

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE, auto_commit=True, verbose=0)

print("=== Advanced Demo ===\n")

print("1. Create table with primary key:")
db['demo'].create_table([
    {'name': 'id', 'type': 'text'},
    {'name': 'name', 'type': 'text', 'default': 'default_name'},
    {'name': 'value', 'type': 'float', 'default': -1}
], pk='id')

print(f"   Primary keys: {db['demo'].pk()}")

print("\n2. Various insert patterns:")
db['demo'].insert({'id': '3', 'value': '333'})  # name uses default
db['demo'].insert({'id': '5', 'name': 7})  # type coercion: name=7 (int->text)
db['demo'].insert({'id': '8', 'name': 100})  # name=100 (int->text)
db['demo'].insert({'id': '9', 'value': 'a'})  # value='a' (string not float)
db['demo'].insert({'id': '10', 'name': None, 'value': 'a'})  # name IS default
db['demo'].insert({'id': '11', 'name': 'NULL', 'value': 'a'})  # name is literal 'NULL'
db['demo'].insert({'id': '12', 'name': '', 'value': 'a'})  # name is empty string

print("\n3. Batch insert with ensure_column (grows schema):")
db['demo'].insert_all([
    {'id': str(i + 13), 'name': str(i), 'value': i * i, 'extra': f'col_{i}'} 
    for i in range(5)
], ensure_column=True)

print(f"   Columns after batch: {list(db['demo'].column_info()['column_type'].keys())}")

print("\n4. Replace vs Ignore:")
db['items'].insert({'id': 1, 'name': 'first'}, pk='id', ensure_table=True)
db['items'].insert({'id': 1, 'name': 'replaced'}, replace=True)
db['items'].insert({'id': 2, 'name': 'original'}, pk='id', ensure_table=True)
db['items'].insert({'id': 2, 'name': 'ignored'}, ignore=True)

print("   After replace/ignore:")
for row in db['items'].select_all('*'):
    print(f"   id={row['id']}, name={row['name']}")

print("\n5. All demo data:")
data = db['demo'].select_all('*')
for row in data:
    print(f"   {row}")

os.remove(DB_FILE)
print("\nDone!")
