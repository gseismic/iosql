"""将 SQLite 目录库迁移到 PostgreSQL 并校验行数。

运行：在本文件所在目录执行 python 11_sqlite_to_postgresql_migration.py。
准备：已安装 iosql；实际迁移还需 PostgreSQL 依赖、14+ 测试库和建表权限。
驱动安装：python -m pip install "psycopg2-binary>=2.9"。
设置：POSTGRES_DSN 可不设（仅 dry-run）；设置后通过 PGPASSWORD 或 PGPASSFILE 提供密码。
目标表原子创建；发生错误时已创建的表可能保留，检查后再处理。
预期：未配置目标时输出 dry-run；配置后输出目标表和校验结果。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

import os
import uuid
from contextlib import closing
from pathlib import Path

from iosql import Database, Partition, Segment


EXAMPLE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = EXAMPLE_DIR / "runtime" / f"11_migration_{uuid.uuid4().hex}"
SOURCE_PATH = RUNTIME_DIR / "source.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"
POSTGRES_DSN = os.environ.get("POSTGRES_DSN")
TARGET_URI = POSTGRES_DSN or "postgresql://user@localhost:5432/market"
SOURCE_TABLE = "ticks"
TARGET_TABLE = os.environ.get(
    "IOSQL_POSTGRES_TARGET_TABLE",
    "iosql_example_migration_" + uuid.uuid4().hex[:12],
)


def check_prerequisites():
    """配置目标时，先检查驱动和数据库连接；未配置时允许 dry-run。"""
    if not POSTGRES_DSN:
        return
    try:
        import psycopg2
    except ImportError:
        raise SystemExit('请先安装 PostgreSQL 驱动：python -m pip install "psycopg2-binary>=2.9"。') from None
    try:
        with closing(psycopg2.connect(POSTGRES_DSN)) as connection:
            if connection.server_version < 140000:
                raise SystemExit("需要 PostgreSQL 14 或更高版本的测试库。")
    except psycopg2.Error:
        raise SystemExit("无法连接 PostgreSQL，请检查测试库、连接参数和凭据。") from None


def reserve_postgresql_table(uri, table_name):
    """原子创建与源契约兼容的空表；同名关系由 PostgreSQL 拒绝。"""
    import psycopg2
    from psycopg2 import sql

    try:
        with closing(psycopg2.connect(uri)) as conn, conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW max_identifier_length")
                if len(table_name.encode("utf-8")) > int(cursor.fetchone()[0]):
                    raise SystemExit("目标表名过长，请选择较短的名称。")
                cursor.execute(sql.SQL(
                    "CREATE TABLE {} (symbol text, ts bigint, price double precision, "
                    "PRIMARY KEY (symbol, ts)) PARTITION BY RANGE (ts)"
                ).format(sql.Identifier(table_name)))
    except psycopg2.Error as error:
        if error.pgcode in ("42P07", "42710", "23505"):
            raise SystemExit(f"目标名称 {table_name} 已存在或被占用，请选择新表名。") from None
        raise


def prepare_source():
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


def run():
    check_prerequisites()
    prepare_source()

    with Database(SOURCE_URI) as source:
        plan = source.migrate_to(
            TARGET_URI,
            tables={SOURCE_TABLE: TARGET_TABLE},
            dry_run=True,
            batch_size=2,
        )
        print("dry-run 源行数:", plan.source_rows, "预计批次:", plan.batches)

        if not POSTGRES_DSN:
            print("未设置 POSTGRES_DSN，已完成 dry-run。")
            print("设置 POSTGRES_DSN 后会执行真实 PostgreSQL 迁移。")
            return

        # 原子占用新表名；默认表名带随机后缀，适合反复运行。
        reserve_postgresql_table(TARGET_URI, TARGET_TABLE)
        result = source.migrate_to(
            TARGET_URI,
            tables={SOURCE_TABLE: TARGET_TABLE},
            verify="row-count",
            batch_size=2,
        )
        print("目标表:", TARGET_TABLE, "迁移行数:", result.rows_copied,
              "行数校验:", result.verified)

    with Database(TARGET_URI) as target:
        table = target.table(TARGET_TABLE)
        rows = table.select(
            ["symbol", "ts", "price"],
            where={"field": "symbol", "op": "==", "value": "AAPL"},
            order_by="ts",
        )
        print("AAPL rows:", rows)
        print("target rows:", table.count_rows())
        print("本地源库:", SOURCE_PATH)


if __name__ == "__main__":
    run()
