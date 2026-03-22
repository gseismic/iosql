import os
import threading
import time
from iosql.utils.mysqlite.connection_pool import DatabaseConnectionPool

DB_FILE = 'examples/runtime/mysqlite/pool_demo.sqlite3'
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)


if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

pool = DatabaseConnectionPool(DB_FILE, max_connections=5)

print("Creating table...")
with pool.get_connection() as conn:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY,
            name TEXT,
            value REAL
        )
    ''')
    conn.execute("INSERT INTO items VALUES (1, 'first', 1.0)")
    conn.execute("INSERT INTO items VALUES (2, 'second', 2.0)")
    conn.commit()

print("Reading from connection pool:")
with pool.get_connection() as conn:
    result = conn.execute("SELECT * FROM items").fetchall()
    for row in result:
        print(f"  {dict(row)}")

print("\nEach thread reads independently (thread-bound connections):")
def worker(thread_id):
    time.sleep(0.01)
    with pool.get_connection() as conn:
        result = conn.execute("SELECT * FROM items").fetchall()
        print(f"  Thread {thread_id}: {len(result)} rows")

threads = []
for i in range(3):
    t = threading.Thread(target=worker, args=(i,))
    threads.append(t)
    t.start()

for t in threads:
    t.join()

print("\nForce closing all connections...")
pool.force_close_all_connections()

print("\nDone!")
