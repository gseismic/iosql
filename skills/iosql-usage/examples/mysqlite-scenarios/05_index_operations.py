"""创建、查询和删除 mysqlite 索引。

运行：在本文件所在目录执行 python 05_index_operations.py。
准备：已安装 iosql 及其默认依赖。
预期：输出创建和删除用户索引的结果；主键约束索引始终保留。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "05_index_ops"
DB_FILE = DB_DIR / "index.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with Database(str(DB_FILE), verbose=0) as db:
        ticks = db["ticks"]
        ticks.insert_all(
            [{"symbol": "AAPL", "ts": t, "price": float(t)} for t in range(3)],
            pk=["symbol", "ts"], ensure_table=True,
        )
        ticks.create_index("ts", exist_ok=True)
        ticks.create_index("price", order="desc", exist_ok=True)
        print("创建后索引:", sorted(ticks.get_all_index()))

        # 按列删除用户索引，不依赖库生成索引名的具体格式。
        dropped = ticks.drop_column_index("price")
        print("按列删除数量:", dropped, "| 剩余:", sorted(ticks.get_all_index()))

        # drop_all_index 只删除用户索引，跳过 SQLite 管理的约束索引。
        ticks.drop_all_index()
        print("清理用户索引后:", sorted(ticks.get_all_index()))
        print("主键:", ticks.pk())
    print("数据位置:", DB_FILE)


if __name__ == "__main__":
    run()
