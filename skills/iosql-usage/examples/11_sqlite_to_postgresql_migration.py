"""SQLite 目录库迁移到 PostgreSQL，并用 row-count 校验结果。"""

import os
from pathlib import Path

from _example_support import reset_database as reset_demo_path

from iosql import Database, Partition, Segment


EXAMPLE_DIR = Path(__file__).resolve().parent
SOURCE_PATH = EXAMPLE_DIR / "runtime" / "11_sqlite_source.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"
POSTGRES_DSN = os.environ.get("POSTGRES_DSN")
TARGET_URI = POSTGRES_DSN or "postgresql://user:pass@localhost:5432/market"
SOURCE_TABLE = "ticks"
TARGET_TABLE = os.environ.get(
    "IOSQL_POSTGRES_TARGET_TABLE",
    "iosql_example_migration_ticks",
)


def prepare_source():
    reset_demo_path(SOURCE_PATH)
    SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(SOURCE_URI) as db:
        ticks = db.table(
            SOURCE_TABLE,
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition=Partition.duration("ts", every="1day"),
            segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
            order_key="ts",
        )
        ticks.write([
            {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
            {"symbol": "MSFT", "ts": 1735689601000, "price": 420.3},
            {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
            {"symbol": "MSFT", "ts": 1735776001000, "price": 421.0},
        ])


def drop_target_table():
    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as exc:
        raise RuntimeError(
            'PostgreSQL 示例需要安装: python -m pip install "iosql[postgresql]"'
        ) from exc

    with psycopg2.connect(TARGET_URI) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier(TARGET_TABLE)
                )
            )


def run():
    prepare_source()

    with Database(SOURCE_URI) as source:
        plan = source.migrate_to(
            TARGET_URI,
            tables={SOURCE_TABLE: TARGET_TABLE},
            dry_run=True,
            batch_size=2,
        )
        print("dry-run:", plan.to_dict())

        if not POSTGRES_DSN:
            print("未设置 POSTGRES_DSN，已完成 dry-run。")
            print("设置 POSTGRES_DSN 后会执行真实 PostgreSQL 迁移。")
            return

        drop_target_table()
        result = source.migrate_to(
            TARGET_URI,
            tables={SOURCE_TABLE: TARGET_TABLE},
            verify="row-count",
            batch_size=2,
        )
        print("migration:", result.to_dict())

    with Database(TARGET_URI) as target:
        print("logical tables:", target.table_names())
        table = target.table(TARGET_TABLE)
        rows = table.select(
            ["symbol", "ts", "price"],
            where={"field": "symbol", "op": "==", "value": "AAPL"},
            order_by="ts",
        )
        print("AAPL rows:", rows)
        print("target rows:", table.count_rows())


if __name__ == "__main__":
    run()
