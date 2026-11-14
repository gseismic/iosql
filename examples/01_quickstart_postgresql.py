"""在 PostgreSQL 写入时序数据并按时间窗口查询。

运行：在本文件所在目录执行 python 01_quickstart_postgresql.py。
准备：已安装 iosql 的 PostgreSQL 依赖；使用 PostgreSQL 14+ 测试库并授予建表权限。
驱动安装：python -m pip install "psycopg2-binary>=2.9"。
设置：POSTGRES_DSN=postgresql://user@localhost:5432/testdb；密码放在 PGPASSWORD 或 PGPASSFILE。
预期：输出提交的记录数和 AAPL 行情，保留 iosql_example_ticks 示例表。
"""

import os
from contextlib import closing

from iosql import Database


def check_prerequisites():
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise SystemExit("请先将 POSTGRES_DSN 设置为 PostgreSQL 测试数据库连接。")
    try:
        import psycopg2
    except ImportError:
        raise SystemExit('请先安装 PostgreSQL 驱动：python -m pip install "psycopg2-binary>=2.9"。') from None
    try:
        with closing(psycopg2.connect(dsn)) as connection:
            if connection.server_version < 140000:
                raise SystemExit("需要 PostgreSQL 14 或更高版本的测试库。")
    except psycopg2.Error:
        raise SystemExit("无法连接 PostgreSQL，请检查测试库、连接参数和凭据。") from None
    return dsn


def run():
    # URI 不必包含密码；驱动会读取 PGPASSWORD 或 PGPASSFILE。
    dsn = check_prerequisites()

    with Database(dsn) as db:
        ticks = db.timeseries(
            "iosql_example_ticks",
            time="ts",
            pk=["symbol", "ts"],
            columns={"symbol": "text", "ts": "integer", "price": "float"},
            partition_every="1day",
        )

        # ignore 跳过重复主键，因此脚本可以重复运行。
        result = ticks.write([
            {"symbol": "AAPL", "ts": 1735689600000, "price": 187.4},
            {"symbol": "AAPL", "ts": 1735776000000, "price": 189.1},
            {"symbol": "MSFT", "ts": 1735776000000, "price": 420.3},
        ], mode="ignore")

        rows = ticks.query(
            symbols="AAPL",
            start=1735689600000,
            end=1735776000000,
            order_by="ts",
        ).to_list()

        print("提交的记录数:", result.rows_seen)
        print("AAPL 查询结果:", rows)


if __name__ == "__main__":
    run()
