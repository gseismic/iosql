"""SQLite 与 PostgreSQL 之间的双向转换示例。"""

import os
from pathlib import Path

from _example_support import reset_database as reset_demo_path

from iosql import Database, Partition, Segment


EXAMPLE_DIR = Path(__file__).resolve().parent
SOURCE_PATH = EXAMPLE_DIR / "runtime" / "12_sqlite_source.iosql"
ROUNDTRIP_PATH = EXAMPLE_DIR / "runtime" / "12_roundtrip_from_postgresql.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"
ROUNDTRIP_URI = f"sqlite://{ROUNDTRIP_PATH}"
POSTGRES_DSN = os.environ.get("POSTGRES_DSN")
PG_TABLE = os.environ.get(
    "IOSQL_POSTGRES_ROUNDTRIP_TABLE",
    "iosql_example_roundtrip_ticks",
)
SQLITE_TABLE = "ticks"
ROUNDTRIP_TABLE = "ticks_from_postgresql"


def prepare_sqlite_source():
    reset_demo_path(SOURCE_PATH)
    SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)

    with Database(SOURCE_URI) as db:
        ticks = db.table(
            SQLITE_TABLE,
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
        return ticks.spec()


def drop_postgresql_table(uri, table_name):
    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as exc:
        raise RuntimeError(
            'PostgreSQL 示例需要安装: python -m pip install "iosql[postgresql]"'
        ) from exc

    with psycopg2.connect(uri) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier(table_name)
                )
            )


def run():
    spec = prepare_sqlite_source()
    reset_demo_path(ROUNDTRIP_PATH)

    with Database(SOURCE_URI) as sqlite_source:
        dry_run = sqlite_source.migrate_to(
            POSTGRES_DSN or "postgresql://user:pass@localhost:5432/market",
            tables={SQLITE_TABLE: PG_TABLE},
            dry_run=True,
            batch_size=2,
        )
        print("sqlite -> postgresql dry-run:", dry_run.to_dict())

        if not POSTGRES_DSN:
            print("未设置 POSTGRES_DSN，已完成 SQLite 源库准备和 dry-run。")
            print("设置 POSTGRES_DSN 后会继续执行 sqlite -> postgresql -> sqlite。")
            return

        drop_postgresql_table(POSTGRES_DSN, PG_TABLE)
        to_pg = sqlite_source.migrate_to(
            POSTGRES_DSN,
            tables={SQLITE_TABLE: PG_TABLE},
            verify="row-count",
            batch_size=2,
        )
        print("sqlite -> postgresql:", to_pg.to_dict())

    with Database(POSTGRES_DSN) as pg_source:
        # PostgreSQL 作为迁移源时，需要给逻辑表绑定同一份表契约。
        pg_source.table(PG_TABLE, spec=spec)
        to_sqlite = pg_source.migrate_to(
            ROUNDTRIP_URI,
            tables={PG_TABLE: ROUNDTRIP_TABLE},
            verify="row-count",
            batch_size=2,
        )
        print("postgresql -> sqlite:", to_sqlite.to_dict())

    with Database(ROUNDTRIP_URI) as roundtrip:
        table = roundtrip.table(ROUNDTRIP_TABLE)
        rows = table.select(
            ["symbol", "ts", "price"],
            where={"field": "symbol", "op": "==", "value": "AAPL"},
            order_by="ts",
            to_dict=True,
        )
        print("roundtrip AAPL rows:", rows)
        print("roundtrip row count:", table.count_rows())


if __name__ == "__main__":
    run()
