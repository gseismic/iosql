"""已有库：读取原表契约，离线重分区到新的目标库。"""

import os
from pathlib import Path

from _example_support import reset_database as reset_demo_path

from iosql import Database


EXAMPLE_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_PATH = EXAMPLE_DIR / "runtime" / "10_existing_source.iosql"
DEFAULT_TARGET_PATH = EXAMPLE_DIR / "runtime" / "10_existing_hourly.iosql"
EXTERNAL_SOURCE_URI = os.environ.get("IOSQL_SOURCE_URI")
EXTERNAL_TARGET_URI = os.environ.get("IOSQL_TARGET_URI")
SOURCE_URI = EXTERNAL_SOURCE_URI or f"sqlite://{DEFAULT_SOURCE_PATH}"
TARGET_URI = EXTERNAL_TARGET_URI or f"sqlite://{DEFAULT_TARGET_PATH}"
TABLE_NAME = os.environ.get("IOSQL_EXISTING_TABLE", "ticks")


def prepare_demo_source():
    """构造并关闭源库，重分区阶段只通过持久化契约重新打开。"""
    if EXTERNAL_SOURCE_URI:
        return
    reset_demo_path(DEFAULT_SOURCE_PATH)
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
    prepare_demo_source()
    if not EXTERNAL_TARGET_URI:
        reset_demo_path(DEFAULT_TARGET_PATH)

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
