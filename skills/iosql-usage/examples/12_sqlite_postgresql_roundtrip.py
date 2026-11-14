"""将 SQLite 数据经 PostgreSQL 迁回新 SQLite 库。

运行：在本文件所在目录执行 python 12_sqlite_postgresql_roundtrip.py。
准备：已安装 iosql；实际迁移还需 PostgreSQL 依赖、14+ 测试库和建表权限。
驱动安装：python -m pip install "psycopg2-binary>=2.9"。
设置：POSTGRES_DSN 可不设（仅 dry-run）；设置后通过 PGPASSWORD 或 PGPASSFILE 提供密码。
目标表原子创建；发生错误时已创建的表可能保留，检查后再处理。
预期：未配置目标时输出 dry-run；配置后输出双向迁移行数和校验结果。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

import os
import uuid
from contextlib import closing
from pathlib import Path

from iosql import Database, Partition, Segment


EXAMPLE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = EXAMPLE_DIR / "runtime" / f"12_roundtrip_{uuid.uuid4().hex}"
SOURCE_PATH = RUNTIME_DIR / "source.iosql"
ROUNDTRIP_PATH = RUNTIME_DIR / "roundtrip.iosql"
SOURCE_URI = f"sqlite://{SOURCE_PATH}"
ROUNDTRIP_URI = f"sqlite://{ROUNDTRIP_PATH}"
POSTGRES_DSN = os.environ.get("POSTGRES_DSN")
PG_TABLE = os.environ.get(
    "IOSQL_POSTGRES_ROUNDTRIP_TABLE",
    "iosql_example_roundtrip_" + uuid.uuid4().hex[:12],
)
SQLITE_TABLE = "ticks"
ROUNDTRIP_TABLE = "ticks_from_postgresql"


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


def prepare_sqlite_source():
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


def run():
    check_prerequisites()
    spec = prepare_sqlite_source()
    with Database(SOURCE_URI) as sqlite_source:
        dry_run = sqlite_source.migrate_to(
            POSTGRES_DSN or "postgresql://user@localhost:5432/market",
            tables={SQLITE_TABLE: PG_TABLE},
            dry_run=True,
            batch_size=2,
        )
        print("sqlite -> postgresql dry-run 源行数:", dry_run.source_rows)

        if not POSTGRES_DSN:
            print("未设置 POSTGRES_DSN，已完成 SQLite 源库准备和 dry-run。")
            print("设置 POSTGRES_DSN 后会继续执行 sqlite -> postgresql -> sqlite。")
            return

        # 原子占用新表名；默认表名带随机后缀，适合反复运行。
        reserve_postgresql_table(POSTGRES_DSN, PG_TABLE)
        to_pg = sqlite_source.migrate_to(
            POSTGRES_DSN,
            tables={SQLITE_TABLE: PG_TABLE},
            verify="row-count",
            batch_size=2,
        )
        print("PostgreSQL 目标表:", PG_TABLE, "迁移行数:", to_pg.rows_copied,
              "行数校验:", to_pg.verified)

    with Database(POSTGRES_DSN) as pg_source:
        # PostgreSQL 作为迁移源时，需要给逻辑表绑定同一份表契约。
        pg_source.table(PG_TABLE, spec=spec)
        to_sqlite = pg_source.migrate_to(
            ROUNDTRIP_URI,
            tables={PG_TABLE: ROUNDTRIP_TABLE},
            verify="row-count",
            batch_size=2,
        )
        print("postgresql -> sqlite 行数:", to_sqlite.rows_copied,
              "行数校验:", to_sqlite.verified)

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
        print("本地演示数据:", RUNTIME_DIR)


if __name__ == "__main__":
    run()
