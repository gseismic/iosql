"""已有库：读取原表契约，离线重分区到新的目标库。"""

import os
import shutil
from pathlib import Path

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
    shutil.rmtree(DEFAULT_SOURCE_PATH, ignore_errors=True)
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
        shutil.rmtree(DEFAULT_TARGET_PATH, ignore_errors=True)

    source_db = Database(
        SOURCE_URI,
        max_open_partitions=5,
        max_connections_per_file=1,
        max_process_connections=32,
    )
    try:
        if TABLE_NAME not in source_db.table_names():
            raise RuntimeError(
                f"源库中不存在表 {TABLE_NAME!r}。请设置 IOSQL_SOURCE_URI "
                "和 IOSQL_EXISTING_TABLE 指向兼容表。"
            )

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
        if source_spec is None or source_spec.partition is None:
            raise RuntimeError("源表必须有持久化契约和分区配置")

        source_rows = source.select("*", where=None, to_dict=True)
        target_spec = source_spec.with_partition(every="1hour")
        result = source.repartition_to(
            TARGET_URI,
            spec=target_spec,
            batch_size=50_000,
        )

        target_db = Database(TARGET_URI)
        try:
            target = target_db.table(TABLE_NAME)
            target_rows = target.select("*", where=None, to_dict=True)
            print("源分区:", source_spec.partition.to_string())
            print("目标分区:", target.spec().partition.to_string())
            print("复制行数:", result.rows_copied)
            print("目标分区键:", result.partitions_created)
            print("源/目标行数:", len(source_rows), len(target_rows))
        finally:
            target_db.close()
    finally:
        source_db.close()


if __name__ == "__main__":
    run()
