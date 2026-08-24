"""mysqlite 场景 02：增加列与删除列。

运行: python examples/mysqlite-scenarios/02_add_drop_column.py
"""

import sqlite3
from pathlib import Path
import shutil

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parents[1] / "runtime" / "mysqlite-scenarios" / "02_add_drop_column"
DB_FILE = DB_DIR / "schema.sqlite3"


def reset_database():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def run():
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise SystemExit(f"需要 SQLite >= 3.35 支持 DROP COLUMN, 当前 {sqlite3.sqlite_version}")

    reset_database()
    db = Database(str(DB_FILE), auto_commit=True, verbose=0)
    events = db["events"]

    print("== ensure_table + ensure_column: 写入时自动演进 schema")
    events.insert({"id": 1, "kind": "click"}, pk="id", ensure_table=True)
    events.insert({"id": 2, "kind": "scroll", "delta_y": 320}, ensure_column=True)
    print("column_type:", dict(events.column_info()["column_type"]))

    print("== add_column / ensure_column 显式加列")
    events.ensure_column("source", "text", default="web")
    added = events.ensure_column("source", "text")
    print("第二次加同名列返回:", added, "(False 表示已存在)")
    events.add_column("score", "integer DEFAULT 0")

    print("== 带默认值回填验证")
    print(events.select_one(["id", "delta_y", "source", "score"], where="id=2"))

    print("== 删除列走原生 SQL（API 无 drop_column）")
    events.execute('ALTER TABLE "events" DROP COLUMN "score"')
    print("删列后 columns:", list(dict(events.column_info()["column_type"]).keys()))

    print("== 被索引的列不能直接删，需先删索引")
    events.create_index("delta_y", exist_ok=True)
    try:
        events.execute('ALTER TABLE "events" DROP COLUMN "delta_y"')
    except Exception as e:
        print(f"预期失败: {type(e).__name__}")
        events.drop_all_index()
        events.execute('ALTER TABLE "events" DROP COLUMN "delta_y"')
        print("先删索引后成功, columns:", list(dict(events.column_info()["column_type"]).keys()))

    db.close()
    print("done ->", DB_FILE)


if __name__ == "__main__":
    run()
