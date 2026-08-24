"""mysqlite：自动建表、动态加列和基础 CRUD。"""

from pathlib import Path
import shutil

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parents[1] / "runtime" / "mysqlite" / "01_schemaless_crud"
DB_FILE = DB_DIR / "demo.sqlite3"


def reset_database():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()

    db = Database(str(DB_FILE), auto_commit=True, verbose=0)
    events = db["events"]

    events.insert({"id": 1, "kind": "click", "page": "/home"}, pk="id", ensure_table=True)
    events.insert({"id": 2, "kind": "scroll", "delta_y": 320}, ensure_column=True)
    events.update({"page": "/pricing"}, where="id=1")
    events.delete(where="id=2")

    rows = events.select_all("*", order_by="id")

    print("表已创建:", events.table_exists())
    print("列:", list(events.column_info()["column_type"].keys()))
    print("剩余行:", rows)
    db.close()
    reset_database()


if __name__ == "__main__":
    run()
