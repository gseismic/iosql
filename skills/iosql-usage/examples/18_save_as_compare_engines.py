"""另存 SQLite 为 Parquet 并比较相同查询。

运行：在本文件所在目录执行 python 18_save_as_compare_engines.py。
准备：已安装 iosql 及其默认依赖。
预期：输出两份数据的行数和查询耗时。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4

from iosql import Database

RUNTIME = Path(__file__).resolve().parent / 'runtime' / f'18_compare_{uuid4().hex}'
SOURCE_PATH = RUNTIME / '18_sqlite_source.iosql'
TARGET_PATH = RUNTIME / '18_parquet_copy.iosql'
BASE_TIME = 1735689600000
ROW_COUNT = 20_000
REPEATS = 5
QUERY = {
    'symbols': 'S00',
    'start': BASE_TIME + 86_400_000,
    'end': BASE_TIME + 90_000_000,
    'columns': ['symbol', 'ts', 'close'],
    'order_by': 'ts',
}


def timed_query(table):
    # 把查询对象构造和完整消费都纳入计时，两端返回相同的 dict 列表。
    started = perf_counter()
    rows = table.query(**QUERY).to_list()
    return rows, (perf_counter() - started) * 1000


def compare_queries(source, target):
    """两端使用相同条件、投影与排序，先验结果再比较重复读取。"""
    tables = {'SQLite': source.table('bars'), 'Parquet': target.table('bars')}
    first = {engine: timed_query(table) for engine, table in tables.items()}
    expected = first['SQLite'][0]
    assert expected and first['Parquet'][0] == expected
    samples = {engine: [] for engine in tables}
    for repeat in range(REPEATS):
        # 交替查询顺序，减少固定先后顺序造成的偏差。
        engines = list(tables) if repeat % 2 == 0 else list(reversed(tables))
        for engine in engines:
            rows, elapsed_ms = timed_query(tables[engine])
            assert rows == expected
            samples[engine].append(elapsed_ms)
    print(f'相同查询返回 {len(expected)} 行，预热后各执行 {REPEATS} 次')
    print(f'{"engine":<10} {"首次查询(ms)":>14} {"预热后中位数(ms)":>16}')
    for engine in tables:
        print(f'{engine:<10} {first[engine][1]:>14.3f} {median(samples[engine]):>16.3f}')
    print('首次查询不代表系统冷缓存；此小样本用于演示，请以实际数据规模和查询负载比较。')


def run():
    RUNTIME.mkdir(parents=True, exist_ok=True)
    source_uri = f'sqlite://{SOURCE_PATH}'
    target_uri = f'parquet://{TARGET_PATH}'
    with Database(source_uri) as source:
        bars = source.timeseries(
            'bars', time='ts', pk=['symbol', 'ts'],
            columns={'symbol': 'text', 'ts': 'integer', 'close': 'float'},
            partition_every='1day',
        )
        rows = [
            {'symbol': f'S{index % 10:02}',
             'ts': BASE_TIME + (index // 10) * 60_000,
             'close': float(index % 1000)}
            for index in range(ROW_COUNT)
        ]
        written = bars.write(rows)
        assert written.rows_written == ROW_COUNT
        preview = source.save_as(target_uri, dry_run=True, verify='row-count')
        assert preview.source_rows == ROW_COUNT and not preview.published
        result = source.save_as(target_uri, verify='row-count')
        assert result.published and result.verified
        assert source.uri == source_uri and bars.uri == source_uri
        assert bars.count_rows() == ROW_COUNT
        print(f'另存完成，源库保留 {bars.count_rows()} 行，目标 {result.target_rows} 行')

    # 重新打开两份数据；复制、行数校验和打开库的时间不计入查询耗时。
    with Database(source_uri) as source, Database(target_uri) as target:
        compare_queries(source, target)
    print('SQLite 源库:', source_uri)
    print('Parquet 副本:', target_uri)


if __name__ == '__main__':
    run()
