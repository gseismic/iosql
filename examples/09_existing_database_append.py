"""已有库：读取持久化契约，不重新声明表结构，继续增量写入。"""

import os
import shutil
from pathlib import Path

from iosql import Database


EXAMPLE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = EXAMPLE_DIR / "runtime" / "09_existing_source.iosql"
EXTERNAL_DB_URI = os.environ.get("IOSQL_EXISTING_URI")
DB_URI = EXTERNAL_DB_URI or f"sqlite://{DEFAULT_DB_PATH}"
TABLE_NAME = os.environ.get("IOSQL_EXISTING_TABLE", "ticks")


def prepare_demo_database():
    """准备阶段结束后关闭连接，后续代码面对的就是一个已有库。"""
    if EXTERNAL_DB_URI:
        return
    shutil.rmtree(DEFAULT_DB_PATH, ignore_errors=True)
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
    prepare_demo_database()

    db = Database(
        DB_URI,
        max_open_partitions=5,
        max_connections_per_file=1,
        max_process_connections=32,
    )
    try:
        if TABLE_NAME not in db.table_names():
            raise RuntimeError(
                f"已有库中不存在表 {TABLE_NAME!r}。请通过 "
                "IOSQL_EXISTING_URI / IOSQL_EXISTING_TABLE 指向兼容的 ticks 表。"
            )

        # 不再传 columns/pk/partition，直接读取库中持久化的 TableSpec。
        ticks = db.table(TABLE_NAME)
        spec = ticks.spec()
        if spec is None:
            raise RuntimeError(f"表 {TABLE_NAME!r} 没有持久化契约，不能使用 write()")

        result = ticks.write(
            {
                "symbol": "AAPL",
                "ts": 1735862400000,
                "price": 191.2,
            },
            mode="ignore",
        )
        latest = ticks.select(
            "*",
            where={"field": "symbol", "op": "==", "value": "AAPL"},
            order_by="ts desc",
            limit=3,
            to_dict=True,
        )

        print("已有契约主键:", spec.primary_key)
        print("已有分区:", spec.partition.to_string() if spec.partition else None)
        print("本次看到/写入:", result.rows_seen, result.rows_written)
        print("AAPL 最新记录:", latest)
        print("进程连接预算:", db.get_process_connection_budget_status())
    finally:
        db.close()


if __name__ == "__main__":
    run()
