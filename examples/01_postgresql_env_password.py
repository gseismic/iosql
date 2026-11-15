"""使用 PGPASSWORD 连接 PostgreSQL 并读写普通表。

运行：在本文件所在目录执行 python 01_postgresql_env_password.py。
准备：已安装 iosql 的 PostgreSQL 依赖；使用 PostgreSQL 14+ 测试库，当前 schema 可建表。
本例创建或重用自己拥有的 iosql_example_env_quotes 普通表，保留数据。
驱动安装：python -m pip install "psycopg2-binary>=2.9"。
设置：POSTGRES_DSN=postgresql://user@localhost:5432/testdb；密码放在 PGPASSWORD。
本例要求密码认证和 PGPASSWORD；取消 PGSERVICE，URI 不包含密码或 service 参数。
预期：输出 AAPL 查询结果，保留 iosql_example_env_quotes 示例表。
"""

import os
from contextlib import closing
from urllib.parse import parse_qsl, urlsplit

from iosql import Database


def check_prerequisites():
    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        raise SystemExit("请设置 POSTGRES_DSN，例如 postgresql://user@localhost:5432/testdb。")
    if not os.environ.get("PGPASSWORD"):
        raise SystemExit("请将 PostgreSQL 密码设置到 PGPASSWORD 环境变量。")

    # URI 中的密码优先于 PGPASSWORD，避免它掩盖此示例的凭据来源。
    uri = urlsplit(dsn)
    params = parse_qsl(uri.query, keep_blank_values=True)
    if uri.password is not None or any(key.lower() == "password" for key, _ in params):
        raise SystemExit("POSTGRES_DSN 不应包含密码，请使用 PGPASSWORD。")
    if os.environ.get("PGSERVICE") or any(key.lower() == "service" for key, _ in params):
        raise SystemExit("本例使用 PGPASSWORD，请取消 PGSERVICE 并移除 URI 的 service 参数。")
    try:
        import psycopg2
    except ImportError:
        raise SystemExit('请先安装 PostgreSQL 驱动：python -m pip install "psycopg2-binary>=2.9"。') from None
    try:
        with closing(psycopg2.connect(dsn)) as connection:
            if connection.server_version < 140000:
                raise SystemExit("需要 PostgreSQL 14 或更高版本的测试库。")
            with connection.cursor() as cursor:
                cursor.execute("SELECT has_schema_privilege(current_schema(), 'CREATE')")
                if not cursor.fetchone()[0]:
                    raise SystemExit("当前 schema 不可建表，请使用有 CREATE 权限的测试 schema。")
                cursor.execute(
                    "SELECT pg_has_role(relowner, 'USAGE') FROM pg_class WHERE oid = to_regclass(%s)",
                    ("iosql_example_env_quotes",),
                )
                owner = cursor.fetchone()
                if owner and not owner[0]:
                    raise SystemExit("本例需要 iosql_example_env_quotes 的所有者权限，请使用自己的测试 schema。")
    except psycopg2.Error:
        raise SystemExit("无法连接 PostgreSQL，请检查测试库、连接参数和 PGPASSWORD。") from None
    return dsn


def run():
    dsn = check_prerequisites()

    # libpq 自动从环境变量读取 PGPASSWORD，代码和 URI 均无需拼接密码。
    with Database(dsn) as db:
        quotes = db.table(
            "iosql_example_env_quotes",
            pk=["symbol"],
            columns={"symbol": "text", "price": "float"},
        )
        quotes.write([
            {"symbol": "AAPL", "price": 187.4},
            {"symbol": "MSFT", "price": 420.3},
        ], mode="ignore")
        rows = quotes.select(where={"field": "symbol", "op": "==", "value": "AAPL"})
        print("AAPL 查询结果:", rows)


if __name__ == "__main__":
    run()
