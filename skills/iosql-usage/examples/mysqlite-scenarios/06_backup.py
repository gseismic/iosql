"""mysqlite 场景 06：在线备份。

运行: python examples/mysqlite-scenarios/06_backup.py
"""

import sqlite3
from pathlib import Path
import shutil

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parents[1] / "runtime" / "mysqlite-scenarios" / "06_backup"
DB_FILE = DB_DIR / "source.sqlite3"
BACKUP_FILE = DB_DIR / "backup.sqlite3"


def reset_database():
    shutil.rmtree(DB_DIR, ignore_errors=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)


def run():
    reset_database()
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
