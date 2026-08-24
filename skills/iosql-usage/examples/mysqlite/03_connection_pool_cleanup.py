"""mysqlite：连接池并发读取和强制释放。"""

from pathlib import Path
import shutil
import threading

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parents[1] / "runtime" / "mysqlite" / "03_pool_cleanup"
DB_FILE = DB_DIR / "demo.sqlite3"


def reset_database():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def seed(db):
    with db.get_connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS items ("
            "id INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL"
            ")"
        )
        conn.execute("INSERT OR REPLACE INTO items (id, name) VALUES (1, 'alpha')")
        conn.execute("INSERT OR REPLACE INTO items (id, name) VALUES (2, 'beta')")
        conn.commit()


def count_rows(db):
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


def run():
    reset_database()

    db = Database(str(DB_FILE), verbose=0, max_connections=4)
    seed(db)

    def worker(worker_id):
        print(f"线程 {worker_id} 行数:", count_rows(db))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    db.force_close_all_connections()
    print("已关闭:", db.is_closed())

    reopened = Database(str(DB_FILE), verbose=0, max_connections=4)
    print("重新打开后行数:", count_rows(reopened))
    reopened.close()
    reset_database()


if __name__ == "__main__":
    run()
