"""PostgreSQL 在线行情：时间窗流式读取、最新截面、计划与字段级更新。"""

import json
import os
import uuid

import psycopg2
from psycopg2 import sql

from iosql import Database


def run():
    dsn = os.environ.get('POSTGRES_DSN')
    if not dsn:
        print('请设置测试数据库 POSTGRES_DSN；需要 PostgreSQL 14+ 与 iosql[postgresql]。')
        return
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
                               columns=['ts', 'close'], order_by='ts')
            rows = [row for batch in query.iter_batches(batch_size=1000) for row in batch]
            assert rows == [{'ts': base, 'close': 187.4}]
            plan = query.explain()
            assert len(plan.scanned_partitions) == 1
            print('时间窗行情：', rows)
            print('脱敏计划：', json.dumps(plan.to_dict(), ensure_ascii=False))
            snapshot = bars.asof(symbols=['MSFT', 'AAPL'], as_of=base + 1,
                                 columns=['symbol', 'close']).to_list()
            assert snapshot == [{'symbol': 'MSFT', 'close': 420.3}, {'symbol': 'AAPL', 'close': 187.4}]
            print('截至时点最新截面：', snapshot)
            bars.upsert_all([{'symbol': 'AAPL', 'ts': base, 'close': 188.0}], pk=['symbol', 'ts'])
            assert bars.query(symbols='AAPL', end=base).one()['venue'] == 'NASDAQ'
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
