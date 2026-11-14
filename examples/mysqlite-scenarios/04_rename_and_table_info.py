"""查看 mysqlite 表信息并用原生 SQL 重命名。

运行：在本文件所在目录执行 python 04_rename_and_table_info.py。
准备：已安装 iosql 及其默认依赖。
预期：输出表结构、重命名结果及元数据注意事项。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "04_rename_and_info"
DB_FILE = DB_DIR / "info.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = Database(str(DB_FILE), auto_commit=True, verbose=0)

    print("== create_table：声明式建表并登记元数据")
    bars = db["bars"]
    bars.create_table(
        [
            {"name": "symbol", "type": "text"},
            {"name": "ts", "type": "integer", "not_null": True},
            {"name": "close", "type": "float", "default": 0.0},
        ],
        pk=["symbol", "ts"],
        table_comment="示例行情表",
        author="iosql",
    )
    bars.insert({"symbol": "AAPL", "ts": 1, "close": 101.5})

    print("== 表信息 API")
    print("get_all_tables:", db.get_all_tables())
    print("table_exists:", bars.table_exists())
    info = bars.column_info()
    print("column_type:", dict(info["column_type"]))
    print("not_null:", info["not_null"])
    print("default:", info["default"])
    print("pk:", bars.pk())

    print("== 重命名走原生 SQL（API 无 rename 方法）")
    bars.execute('ALTER TABLE "bars" RENAME TO "bars_2026"')
    renamed = db["bars_2026"]
    print("重命名后 tables:", db.get_all_tables())
    print("旧名 table_exists:", db["bars"].table_exists(),
          "| 新名:", renamed.table_exists(), "| 行数:", renamed.count())

    print("== 注意: 原生 RENAME 不会同步 _meta，需要自行维护")
    meta = db["_meta"]
    for row in meta.query_all("SELECT table_name, pk, table_comment FROM _meta"):
        print("_meta 行:", dict(row))

    db.close()
    print("done ->", DB_FILE)


if __name__ == "__main__":
    run()
