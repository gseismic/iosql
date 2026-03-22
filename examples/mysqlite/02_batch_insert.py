from pathlib import Path
import shutil

from iosql.utils.mysqlite import Database

DB_DIR = Path('examples/runtime/mysqlite/batch_insert')
DB_FILE = DB_DIR / 'batch_demo.sqlite3'


def _reset_db():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


_reset_db()
db = Database(str(DB_FILE), auto_commit=True)

db['events'].create_table([
    {'name': 'id', 'type': 'INTEGER PRIMARY KEY'},
    {'name': 'event_type', 'type': 'TEXT'},
    {'name': 'value', 'type': 'REAL'},
    {'name': 'timestamp', 'type': 'INTEGER'},
])

records = [
    {'event_type': 'click', 'value': 1.5, 'timestamp': 1000 + i}
    for i in range(100)
]

print(f"Inserting {len(records)} records...")
db['events'].insert_all(records)
print(f"Done! Total records: {len(db['events'].select_all('*'))}")

print("\nFirst 5 records:")
for row in db['events'].select_all('*', limit=5):
    print(f"  {row}")

print("\nCount by event_type:")
for row in db['events'].query_all("SELECT event_type, COUNT(*) as cnt FROM events GROUP BY event_type"):
    print(f"  {row}")

print("\nDone!")
