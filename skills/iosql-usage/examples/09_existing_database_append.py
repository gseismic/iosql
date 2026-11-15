"""重开已有表并复用持久化契约增量写入。

运行：在本文件所在目录执行 python 09_existing_database_append.py。
准备：已安装 iosql 及其默认依赖。
可选 IOSQL_EXISTING_URI 和 IOSQL_EXISTING_TABLE；仅用于已有 SQLite/Parquet 测试库，
表须含 symbol、ts、price 字段；设置后会写入示例记录。
预期：输出已有契约、写入结果及最新记录。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

import os
from uuid import uuid4
from pathlib import Path

from iosql import Database


EXAMPLE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = EXAMPLE_DIR / "runtime" / uuid4().hex / "09_existing_source.iosql"
EXTERNAL_DB_URI = os.environ.get("IOSQL_EXISTING_URI")
DB_URI = EXTERNAL_DB_URI or f"sqlite://{DEFAULT_DB_PATH}"
TABLE_NAME = os.environ.get("IOSQL_EXISTING_TABLE", "ticks")


def check_prerequisites():
    if EXTERNAL_DB_URI:
        with Database(EXTERNAL_DB_URI) as db:
            if db.engine not in ("sqlite", "parquet"):
                raise SystemExit("本例重开已有 SQLite/Parquet 库；PostgreSQL 重开契约请参考往返迁移示例。")
            if TABLE_NAME not in db.table_names():
                raise SystemExit("IOSQL_EXISTING_URI 中未找到指定表，请设置已有测试库及表名。")
            spec = db.table(TABLE_NAME).spec()
            if spec is None or not {"symbol", "ts", "price"}.issubset(spec.columns):
                raise SystemExit("已有测试表须保存含 symbol、ts、price 字段的契约。")


def prepare_demo_database():
    """准备阶段结束后关闭连接，后续代码面对的就是一个已有库。"""
    if EXTERNAL_DB_URI:
        return
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(DB_URI) as db:
        ticks = db.table(
            TABLE_NAME,
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition="duration(ts,every=1day)",
            order_key="ts",
        )
        ticks.write([
            {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
            {"symbol": "MSFT", "ts": 1735689600000, "price": 420.3},
        ])


def run():
    check_prerequisites()
    prepare_demo_database()

    with Database(DB_URI) as db:
        ticks = db.table(TABLE_NAME)
        spec = ticks.spec()

        result = ticks.write(
            {
                "symbol": "AAPL",
                "ts": 1735862400000,
                "price": 191.2,
            },
            mode="ignore",
        )
        latest = ticks.select(
            where={"field": "symbol", "op": "==", "value": "AAPL"},
            order_by="ts desc",
            limit=3,
            to_dict=True,
        )

        print("已有契约主键:", spec.primary_key)
        print("已有分区:", spec.partition.to_string() if spec.partition else None)
        print("本次看到/写入:", result.rows_seen, result.rows_written)
        print("AAPL 最新记录:", latest)


if __name__ == "__main__":
    run()
