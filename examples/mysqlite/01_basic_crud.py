import os
from iosql.utils.mysqlite import Database

DB_FILE = 'examples/data/demo.sqlite3'

if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

db = Database(DB_FILE, auto_commit=True)

db['users'].create_table([
    {'name': 'id', 'type': 'INTEGER PRIMARY KEY'},
    {'name': 'name', 'type': 'TEXT', 'default': 'unknown'},
    {'name': 'age', 'type': 'INTEGER', 'default': 0},
    {'name': 'email', 'type': 'TEXT'},
])

db['users'].insert({'id': 1, 'name': 'Alice', 'age': 30, 'email': 'alice@example.com'})
db['users'].insert({'id': 2, 'name': 'Bob', 'age': 25, 'email': 'bob@example.com'})
db['users'].insert({'id': 3, 'name': 'Charlie', 'age': 35, 'email': 'charlie@example.com'})

print("All users:")
for row in db['users'].select_all('*'):
    print(f"  {row}")

print("\nUser with id=2:")
user = db['users'].select_one('*', where='id=2')
print(f"  {user}")

print("\nUsers older than 28:")
for row in db['users'].select_all('*', where='age > 28'):
    print(f"  {row}")

db['users'].update({'age': 31}, where='id=1')
print("\nAfter update (Alice age -> 31):")
print(f"  {db['users'].select_one('*', where='id=1')}")

db['users'].delete(where='id=3')
print("\nAfter delete (Charlie removed):")
print(f"  Total users: {db['users'].count()}")

print("\nAll table names in database:")
print(f"  {db.tables()}")

print("\nDone!")
