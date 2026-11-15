"""读取已有表契约并重分区到独立目标库。

运行：在本文件所在目录执行 python 10_existing_database_repartition.py。
准备：已安装 iosql 及其默认依赖。
可选 IOSQL_SOURCE_URI、IOSQL_TARGET_URI 和 IOSQL_EXISTING_TABLE；使用 SQLite/Parquet 库。
源表须保存以毫秒时间戳 ts 分区的契约，目标库必须为空且与源库独立。
预期：输出源和目标的分区及行数。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

import os
from pathlib import Path
from uuid import uuid4

from iosql import Database


EXAMPLE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = EXAMPLE_DIR / "runtime" / f"10_repartition_{uuid4().hex}"
DEFAULT_SOURCE_PATH = RUNTIME_DIR / "source.iosql"
DEFAULT_TARGET_PATH = RUNTIME_DIR / "hourly.iosql"
EXTERNAL_SOURCE_URI = os.environ.get("IOSQL_SOURCE_URI")
EXTERNAL_TARGET_URI = os.environ.get("IOSQL_TARGET_URI")
SOURCE_URI = EXTERNAL_SOURCE_URI or f"sqlite://{DEFAULT_SOURCE_PATH}"
TARGET_URI = EXTERNAL_TARGET_URI or f"sqlite://{DEFAULT_TARGET_PATH}"
TABLE_NAME = os.environ.get("IOSQL_EXISTING_TABLE", "ticks")


def check_prerequisites():
    if EXTERNAL_SOURCE_URI:
        with Database(EXTERNAL_SOURCE_URI) as source:
            if source.engine not in ("sqlite", "parquet"):
                raise SystemExit("本例重开已有 SQLite/Parquet 库；PostgreSQL 需要显式重新绑定契约。")
            if TABLE_NAME not in source.table_names():
                raise SystemExit("IOSQL_SOURCE_URI 中未找到指定表，请设置已有源库及表名。")
            spec = source.table(TABLE_NAME).spec()
            if (spec is None or spec.time is None or spec.time.name != "ts"
                    or spec.time.unit != "ms"):
                raise SystemExit("源测试表须保存 time=ts、unit=ms 的时序契约。")
    with Database(TARGET_URI) as target:
        if target.engine not in ("sqlite", "parquet"):
            raise SystemExit("本例目标须为独立的新 SQLite/Parquet 库。")
        if target.table_names():
            raise SystemExit("目标库必须为空，请将 IOSQL_TARGET_URI 设置为独立的新库。")


def prepare_demo_source():
    """构造并关闭源库，重分区阶段只通过持久化契约重新打开。"""
    if EXTERNAL_SOURCE_URI:
        return
    DEFAULT_SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(SOURCE_URI) as db:
        ticks = db.table(
            TABLE_NAME,
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition="duration(ts,every=1day)",
            order_key="ts",
        )
        ticks.write([
            {
                "symbol": "AAPL",
                "ts": 1735689600000 + hour * 3_600_000,
                "price": 187.0 + hour,
            }
            for hour in range(4)
        ])


def run():
    check_prerequisites()
    prepare_demo_source()

    with Database(SOURCE_URI) as source_db:
        source = source_db.table(TABLE_NAME)
        source_spec = source.spec()
        source_rows = source.count_rows()
        target_spec = source_spec.with_partition(partition="duration(ts,every=1hour)")
        result = source.repartition_to(
            TARGET_URI,
            spec=target_spec,
            batch_size=50_000,
        )

        with Database(TARGET_URI) as target_db:
            target = target_db.table(TABLE_NAME)
            target_rows = target.count_rows()
            print("源分区:", source_spec.partition.to_string() if source_spec.partition else None)
            print("目标分区:", target.spec().partition.to_string())
            print("复制行数:", result.rows_copied)
            print("目标分区键:", result.partitions_created)
            print("源/目标行数:", source_rows, target_rows)


if __name__ == "__main__":
    run()
