from pathlib import Path
import shutil

from iosql.utils.mysqlite import Database

DB_DIR = Path('examples/runtime/mysqlite/demo_basic')
DB_FILE = DB_DIR / 'demo_basic.sqlite3'


def _reset_db():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


_reset_db()
db = Database(str(DB_FILE), auto_commit=True, verbose=0)

print("=== Basic Demo ===\n")

print("1. Create table with defaults:")
db['users'].create_table([
    {'name': 'id', 'type': 'text'},
    {'name': 'name', 'type': 'text', 'default': 'default_name'},
    {'name': 'value', 'type': 'float', 'default': -1}
], pk='id')

print(f"   default_values: {db['users'].default_values()}")

print("\n2. Insert with default value handling:")
db['users'].insert({'id': '0', 'value': '333'})  # name uses default
db['users'].insert({'id': '1', 'name': None, 'value': 'a'})  # name IS default value
db['users'].insert({'id': '2', 'name': '', 'value': 'a'})  # name NOT default value (empty string)

print("\n3. Batch insert with ensure_column (auto-add new columns):")
db['users'].insert_all([
    {'id': str(i + 10), 'name': str(i), 'value': i * i} for i in range(3)
], ensure_column=True)

print("\n4. Batch insert without table (will fail):")
try:
    db['logs'].insert_all([{'id': 1, 'msg': 'test'}])
except Exception as e:
    print(f"   Error (expected): {e}")

print("\n5. Select all:")
data = db['users'].select_all('*')
for row in data:
    print(f"   {row}")

print(f"\nTotal rows: {db['users'].count()}")

_reset_db()
print("\nDone!")
