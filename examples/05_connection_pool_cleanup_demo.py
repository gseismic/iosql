"""Connection pool cleanup demo.

This script creates thread-bound SQLite connections, forces cleanup, and then
reopens the same database to prove the pool was released cleanly.
"""

from pathlib import Path
import shutil
import threading

from iosql.utils.mysqlite import Database

DB_DIR = Path('examples/runtime/partition/connection_pool_cleanup')
DB_PATH = DB_DIR / 'cleanup.sqlite3'


def _remove_existing_db():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def _bootstrap(db: Database):
    with db.get_connection() as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS items ('
            'id INTEGER PRIMARY KEY, '
            'name TEXT NOT NULL'
            ')'
        )
        conn.execute("INSERT OR REPLACE INTO items (id, name) VALUES (1, 'alpha')")
        conn.execute("INSERT OR REPLACE INTO items (id, name) VALUES (2, 'beta')")
        conn.commit()


def _count_rows(db: Database) -> int:
    with db.get_connection() as conn:
        return conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]


def run():
    _remove_existing_db()

    db = Database(str(DB_PATH), verbose=1, max_connections=4)
    _bootstrap(db)

    print('Initial row count:', _count_rows(db))

    def worker(thread_id: int):
        with db.get_connection() as conn:
            count = conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]
            print(f'Thread {thread_id}: {count} rows')

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    print('Forcing cleanup...')
    db.force_close_all_connections()
    print('Database closed:', db.is_closed())

    reopened = Database(str(DB_PATH), verbose=1, max_connections=4)
    print('Row count after reopen:', _count_rows(reopened))
    reopened.close()

    _remove_existing_db()
    print('Cleanup demo finished.')


if __name__ == '__main__':
    run()
