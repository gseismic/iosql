"""创建、查询和删除 mysqlite 索引。

运行：在本文件所在目录执行 python 05_index_operations.py。
准备：已安装 iosql 及其默认依赖。
预期：输出索引变化及约束索引不能删除的提示。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

import sqlite3
from pathlib import Path
from uuid import uuid4

from iosql.utils.mysqlite import Database


DB_DIR = Path(__file__).resolve().parent / "runtime" / uuid4().hex / "05_index_ops"
DB_FILE = DB_DIR / "index.sqlite3"


def run():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    db = Database(str(DB_FILE), auto_commit=True, verbose=0)
    ticks = db["ticks"]
    ticks.create_table(
        [
            {"name": "symbol", "type": "text"},
            {"name": "ts", "type": "integer"},
            {"name": "price", "type": "float"},
        ],
        pk=["symbol", "ts"],
    )
    ticks.insert_all(
        [{"symbol": f"S{i}", "ts": t, "price": float(t)} for i in range(3) for t in range(100)]
    )

    print("== Table.create_index：单列索引（exist_ok 幂等）")
    ticks.create_index("ts", exist_ok=True)
    ticks.create_index("price", order="desc", exist_ok=True)

    print("== Table.get_all_index：本表索引名 / 完整信息")
    print("names:", sorted(ticks.get_all_index()))
    full = ticks.get_all_index(full_info=True)
    print("sql 样例:", next(item["sql"] for item in full if item["name"].endswith("ts_index")))

    print("== Database 级索引查询 API")
    print("db.get_table_indices('ticks'):", sorted(db.get_table_indices("ticks")))
    print("db.get_all_indices():", len(db.get_all_indices()), "个")

    print("== drop_column_index：按列反查并删除索引")
    dropped = ticks.drop_column_index("price")
    print("按列删除数量:", dropped, "| 剩余:", sorted(ticks.get_all_index()))

    print("== drop_index / drop_all_index")
    ts_index = [n for n in ticks.get_all_index() if n.endswith("ts_index")]
    ticks.drop_index(ts_index[0])
    print("删单个后:", sorted(ticks.get_all_index()))

    print("== drop_all_index 不过滤主键 autoindex，SQLite 禁止删除约束索引")
    try:
        ticks.drop_all_index()
    except sqlite3.OperationalError as e:
        print(f"预期失败: {e}")
        for name in [n for n in ticks.get_all_index() if "autoindex" not in n.lower()]:
            ticks.drop_index(name)
        print("手动清完用户索引后:", sorted(ticks.get_all_index()))

    db.close()
    print("done ->", DB_FILE)


if __name__ == "__main__":
    run()
