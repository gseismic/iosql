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
    with Database(str(DB_FILE), verbose=0) as db:
        bars = db["bars"]
        bars.create_table(
            [
                {"name": "symbol", "type": "text"},
                {"name": "ts", "type": "integer", "not_null": True},
                {"name": "close", "type": "float", "default": 0.0},
            ],
            pk=["symbol", "ts"],
            table_comment="示例行情表",
        )
        bars.insert({"symbol": "AAPL", "ts": 1, "close": 101.5})
        print("重命名前字段:", dict(bars.column_info()["column_type"]))

        # 低层工具没有 rename 方法，使用原生 SQL，之后用新名字取得 Table。
        bars.execute('ALTER TABLE "bars" RENAME TO "bars_2026"')
        renamed = db["bars_2026"]
        print("重命名后表清单:", db.get_all_tables())
        print("新表记录:", renamed.select_all("*"))

        # 原生 RENAME 不同步 _meta；这里展示该限制，依赖业务元数据时需另行维护。
        print("原元数据:", db["_meta"].query_all("SELECT table_name, table_comment FROM _meta"))
    print("数据位置:", DB_FILE)


if __name__ == "__main__":
    run()
