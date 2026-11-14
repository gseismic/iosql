"""读取已有表契约并重分区到独立目标库。

运行：在本文件所在目录执行 python 10_existing_database_repartition.py。
准备：已安装 iosql 及其默认依赖。
可选 IOSQL_SOURCE_URI、IOSQL_TARGET_URI 和 IOSQL_EXISTING_TABLE；目标库必须为空。
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
            if TABLE_NAME not in source.table_names():
                raise SystemExit("IOSQL_SOURCE_URI 中未找到指定表，请设置已有源库及表名。")


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

    with Database(
        SOURCE_URI,
        max_open_partitions=5,
        max_connections_per_file=1,
        max_process_connections=32,
    ) as source_db:
        # spec() 缺失时 repartition_to 会直接报错并给出指引，
        # 不需要手动防御。
        target_probe = Database(TARGET_URI)
        try:
            if target_probe.table_names():
                raise RuntimeError(
                    "目标库必须为空。重分区不会原地覆盖；请设置新的 "
                    "IOSQL_TARGET_URI，或确认后手动清理旧目标。"
                )
        finally:
            target_probe.close()

        source = source_db.table(TABLE_NAME)
        source_spec = source.spec()
        source_rows = source.select(to_dict=True)
        target_spec = source_spec.with_partition(partition="duration(ts,every=1hour)")
        result = source.repartition_to(
            TARGET_URI,
            spec=target_spec,
            batch_size=50_000,
        )

        with Database(TARGET_URI) as target_db:
            target = target_db.table(TABLE_NAME)
            target_rows = target.select(to_dict=True)
            print("源分区:", source_spec.partition.to_string())
            print("目标分区:", target.spec().partition.to_string())
            print("复制行数:", result.rows_copied)
            print("目标分区键:", result.partitions_created)
            print("源/目标行数:", len(source_rows), len(target_rows))


if __name__ == "__main__":
    run()
