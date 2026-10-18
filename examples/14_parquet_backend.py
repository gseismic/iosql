"""Parquet 分区存储、惰性排序、重开与 SQLite 往返迁移。"""

from pathlib import Path

from _example_support import reset_database as reset_demo_path

from iosql import Database, Segment


RUNTIME = Path(__file__).resolve().parent / 'runtime'
PARQUET_PATH = RUNTIME / '14_parquet.iosql'
SQLITE_PATH = RUNTIME / '14_parquet_sqlite.iosql'
RESTORED_PATH = RUNTIME / '14_parquet_restored.iosql'
BASE_TIME = 1735689600000


def run():
    for path in [PARQUET_PATH, SQLITE_PATH, RESTORED_PATH]:
        reset_demo_path(path)

    uri = f'parquet://{PARQUET_PATH}'
    with Database(uri) as db:
        bars = db.timeseries(
            'bars', time='ts', pk=['symbol', 'ts'],
            columns={'symbol': 'text', 'ts': 'integer', 'close': 'float'},
            partition_every='1day', segment=Segment.rows(max_rows=2),
            description='历史行情 Parquet 归档',
        )
        with bars.buffered_writer(max_rows=3) as writer:
            writer.append([
                {'symbol': 'AAPL', 'ts': BASE_TIME + 2, 'close': 187.4},
                {'symbol': 'AAPL', 'ts': BASE_TIME, 'close': 187.0},
                {'symbol': 'MSFT', 'ts': BASE_TIME + 1, 'close': 410.0},
                {'symbol': 'AAPL', 'ts': BASE_TIME + 86_400_000, 'close': 188.0},
            ])
        query = bars.query(symbols='AAPL', order_by='ts')
        print('有序行情:', list(query.iter_rows(batch_size=2)))
        print('裁剪计划:', query.explain().to_dict())
        print('物理 Parquet 文件:', [segment['path'] for segment in bars.segments()])
        assert bars.validate(deep=True).ok

    with Database(uri) as db:
        bars = db.table('bars')
        assert bars.count_rows() == 4
        target_uri = f'sqlite://{SQLITE_PATH}'
        print('迁移预览:', db.migrate_to(target_uri, dry_run=True).to_dict())
        result = db.migrate_to(target_uri, verify='row-count', batch_size=2)
        assert result.verified
        print('迁移结果:', result.to_dict())

    with Database(f'sqlite://{SQLITE_PATH}') as db:
        assert db.table('bars').count_rows() == 4
        result = db.migrate_to(f'parquet://{RESTORED_PATH}', verify='row-count', batch_size=2)
        assert result.verified

    with Database(f'parquet://{RESTORED_PATH}') as db:
        assert db.table('bars').count_rows() == 4
        assert db.table('bars').validate(deep=True).ok
    print('Parquet → SQLite → Parquet 往返校验通过')


if __name__ == '__main__':
    run()
