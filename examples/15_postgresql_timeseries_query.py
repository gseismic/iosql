"""查看 PostgreSQL 时序查询和执行计划。

运行：在本文件所在目录执行 python 15_postgresql_timeseries_query.py。
准备：已安装 iosql 的 PostgreSQL 依赖；使用 PostgreSQL 14+ 测试库并授予建表权限。
驱动安装：python -m pip install "psycopg2-binary>=2.9"。
设置：POSTGRES_DSN=postgresql://user@localhost:5432/testdb；密码放在 PGPASSWORD 或 PGPASSFILE。
预期：输出行情、执行计划及耗时，并清理本例随机表。
"""

import json
import os
import uuid
from contextlib import closing

from iosql import Database


def check_prerequisites():
    dsn = os.environ.get('POSTGRES_DSN')
    if not dsn:
        raise SystemExit(
            '请将 POSTGRES_DSN 设置为 PostgreSQL 测试数据库连接；'
            '需要 PostgreSQL 14+ 与 iosql[postgresql]。'
        )
    try:
        import psycopg2
    except ImportError:
        raise SystemExit('请先安装 PostgreSQL 驱动：python -m pip install "psycopg2-binary>=2.9"。') from None
    try:
        with closing(psycopg2.connect(dsn)) as connection:
            if connection.server_version < 140000:
                raise SystemExit("需要 PostgreSQL 14 或更高版本的测试库。")
    except psycopg2.Error:
        raise SystemExit('无法连接 PostgreSQL，请检查测试库、连接参数和凭据。') from None
    return dsn


def run():
    dsn = check_prerequisites()
    import psycopg2
    from psycopg2 import sql

    name = 'iosql_example_query_' + uuid.uuid4().hex[:12]
    base = 1735689600000
    try:
        with Database(dsn) as db:
            bars = db.timeseries(
                name, time='ts', pk=['symbol', 'ts'], partition_every='1day',
                columns={'symbol': 'text', 'ts': 'integer', 'close': 'float', 'venue': 'text'},
            )
            bars.write([
                {'symbol': 'AAPL', 'ts': base, 'close': 187.4, 'venue': 'NASDAQ'},
                {'symbol': 'MSFT', 'ts': base + 1, 'close': 420.3, 'venue': 'NASDAQ'},
                {'symbol': 'AAPL', 'ts': base + 86_400_000, 'close': 189.1, 'venue': 'NASDAQ'},
            ])
            # 主键已提供 (symbol, ts)，此调用复用已有索引。
            bars.create_index(columns=['symbol', 'ts'])
            bars.create_index(columns=['venue', 'ts'])
            query = bars.query(symbols='AAPL', start=base, end=base + 1,
                               where={'field': 'venue', 'op': '==', 'value': 'NASDAQ'},
                               columns=['ts', 'close'], order_by='ts')
            rows = [row for batch in query.iter_batches(batch_size=1000) for row in batch]
            assert rows == [{'ts': base, 'close': 187.4}]
            plan = query.explain()
            assert len(plan.scanned_partitions) == 1
            print('时间窗行情：', rows)
            print('脱敏计划：', json.dumps(plan.to_dict(), ensure_ascii=False))
            # 小样本上 PostgreSQL 也可能选择顺序扫描；应以真实负载观察计划。
            measured = query.explain(analyze=True)
            assert measured.execution_time_ms is not None
            print('实际执行耗时（毫秒）：', measured.execution_time_ms)
            bars.drop_index(columns=['venue', 'ts'])
    finally:
        # 只清理由此示例生成的随机表，避免重建用户已有表。
        with psycopg2.connect(dsn) as conn, conn.cursor() as cursor:
            cursor.execute(sql.SQL('DROP TABLE IF EXISTS {} CASCADE').format(sql.Identifier(name)))
    print('PostgreSQL 查询示例校验通过')


if __name__ == '__main__':
    run()
