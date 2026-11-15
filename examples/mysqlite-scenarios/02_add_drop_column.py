"""使用 mysqlite 增列并用 SQLite 原生 SQL 删列。

运行：在本文件所在目录执行 python 02_add_drop_column.py。
准备：已安装 iosql 及其默认依赖。
需要 SQLite 3.35 或更高版本以支持 DROP COLUMN。
预期：输出默认值、删除列并刷新缓存后的表结构。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

import sqlite3
from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "02_add_drop_column"
DB_FILE = DB_DIR / "schema.sqlite3"


def check_prerequisites():
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise SystemExit(f"需要 SQLite >= 3.35 支持 DROP COLUMN, 当前 {sqlite3.sqlite_version}")


def run():
    check_prerequisites()
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with Database(str(DB_FILE), verbose=0) as db:
        events = db["events"]
        events.insert({"id": 1, "kind": "click"}, pk="id", ensure_table=True)
        events.insert({"id": 2, "kind": "scroll", "delta_y": 320}, ensure_column=True)
        events.ensure_column("source", "text", default="web")
        events.add_column("score", "integer DEFAULT 0")
        print("新增列默认值:", events.select_one(["id", "source", "score"], where="id=2"))

        # 原生 DDL 不会刷新 Table 缓存；再次查看字段前显式失效缓存。
        print("删除前字段:", list(events.column_info()["column_type"]))
        events.execute('ALTER TABLE "events" DROP COLUMN "score"')
        events.invalidate_cache()
        print("删除 score 后字段:", list(events.column_info()["column_type"]))

        # 删除有用户索引的列，先移除该列索引；主键等其他依赖仍需自行处理。
        events.create_index("delta_y")
        events.drop_column_index("delta_y")
        events.execute('ALTER TABLE "events" DROP COLUMN "delta_y"')
        events.invalidate_cache()
        print("最终字段:", list(events.column_info()["column_type"]))
    print("数据位置:", DB_FILE)


if __name__ == "__main__":
    run()
