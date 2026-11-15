"""使用 mysqlite 连接池并发读取并释放连接。

运行：在本文件所在目录执行 python 03_connection_pool_cleanup.py。
准备：已安装 iosql 及其默认依赖。
预期：输出各线程行数、关闭状态和重开后的行数。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "03_pool_cleanup"
DB_FILE = DB_DIR / "demo.sqlite3"


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
    DB_DIR.mkdir(parents=True, exist_ok=True)

    with Database(str(DB_FILE), verbose=0, max_connections=4) as db:
        seed(db)

        # 消费 Future 的结果，读取失败会传回主线程，不会被误报为成功。
        with ThreadPoolExecutor(max_workers=3) as pool:
            counts = list(pool.map(count_rows, [db] * 3))
        print("各线程行数:", counts)

        db.force_close_all_connections()
    print("已关闭:", db.is_closed())

    with Database(str(DB_FILE), verbose=0, max_connections=4) as reopened:
        print("重新打开后行数:", count_rows(reopened))


if __name__ == "__main__":
    run()
