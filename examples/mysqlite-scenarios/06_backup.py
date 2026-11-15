"""使用 SQLite backup API 备份 mysqlite 数据库。

运行：在本文件所在目录执行 python 06_backup.py。
准备：已安装 iosql 及其默认依赖。
预期：输出源库和备份库行数及独立性检查。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "06_backup"
DB_FILE = DB_DIR / "source.sqlite3"
BACKUP_FILE = DB_DIR / "backup.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with Database(str(DB_FILE), verbose=0) as db:
        events = db["events"]
        events.insert_all(
            [{"id": i, "kind": f"k{i}"} for i in range(3)],
            pk="id", ensure_table=True,
        )

        # 仅演示低层单文件库；iosql 目录库使用主示例中的 iosql backup。
        with db.get_connection() as src_conn, closing(sqlite3.connect(str(BACKUP_FILE))) as dst_conn:
            src_conn.backup(dst_conn)

        with Database(str(BACKUP_FILE), verbose=0) as backup_db:
            backup_rows = backup_db["events"].count()
            print(f"源库 {events.count()} 行 / 备份 {backup_rows} 行")
            events.insert({"id": 1000, "kind": "new"})
            print(f"继续写入后源库 {events.count()} 行 / 备份仍 {backup_db['events'].count()} 行")
    print("备份位置:", BACKUP_FILE)


if __name__ == "__main__":
    run()
