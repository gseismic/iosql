"""使用 SQLite backup API 备份 mysqlite 数据库。

运行：在本文件所在目录执行 python 06_backup.py。
准备：已安装 iosql 及其默认依赖。
预期：输出源库和备份库行数及独立性检查。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

import sqlite3
from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "06_backup"
DB_FILE = DB_DIR / "source.sqlite3"
BACKUP_FILE = DB_DIR / "backup.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = Database(str(DB_FILE), auto_commit=True, verbose=0)
    events = db["events"]
    events.insert_all(
        [{"id": i, "kind": f"k{i}"} for i in range(100)],
        pk="id",
        ensure_table=True,
    )

    print("== 通过池化连接调用 sqlite3 backup API（在线快照，不阻塞写入语义）")
    with db.get_connection() as src_conn:
        dst_conn = sqlite3.connect(str(BACKUP_FILE))
        try:
            src_conn.backup(dst_conn)
            dst_conn.commit()
        finally:
            dst_conn.close()

    print("== 打开备份库校验一致性")
    backup_db = Database(str(BACKUP_FILE), auto_commit=True, verbose=0)
    backup_rows = backup_db["events"].count()
    print(f"源库 {events.count()} 行 / 备份 {backup_rows} 行 -> "
          f"{'一致' if backup_rows == events.count() else '不一致'}")
    print("备份库表清单:", sorted(backup_db.get_all_tables()))

    print("== 备份是独立文件：继续写源库不影响备份")
    events.insert({"id": 1000, "kind": "new"}, ensure_column=False)
    print(f"源库 {events.count()} 行 / 备份仍 {backup_db['events'].count()} 行")

    backup_db.close()
    db.close()
    print("done ->", BACKUP_FILE)


if __name__ == "__main__":
    run()
