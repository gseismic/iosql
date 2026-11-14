"""将 SQLite 时序表迁移到 Parquet。

运行：在本文件所在目录执行 python 16_sqlite_to_parquet.py。
准备：已安装 iosql 及其默认依赖。
预期：输出迁移预览、校验结果和 Parquet 中的行情。
数据：本地演示数据保留在文件旁 runtime/ 的独立目录中。
"""

from pathlib import Path
from uuid import uuid4

from iosql import Database


RUNTIME = Path(__file__).resolve().parent / 'runtime' / f'16_migration_{uuid4().hex}'
SQLITE_PATH = RUNTIME / '16_sqlite_source.iosql'
PARQUET_PATH = RUNTIME / '16_parquet_archive.iosql'
BASE_TIME = 1735689600000
SOURCE_ROWS = [
    {'symbol': 'AAPL', 'ts': BASE_TIME, 'close': 187.0},
    {'symbol': 'AAPL', 'ts': BASE_TIME + 86_400_000, 'close': 188.0},
    {'symbol': 'MSFT', 'ts': BASE_TIME + 86_400_001, 'close': 410.0},
    {'symbol': 'AAPL', 'ts': BASE_TIME + 2 * 86_400_000, 'close': 189.0},
]


def migrate_sqlite_to_parquet(source_uri, target_uri, tables=None, specs=None):
    """迁移已有 iosql SQLite 目录库到新 Parquet 库；目标库应为空。

    例如：migrate_sqlite_to_parquet(
        'sqlite://db/market.iosql', 'parquet://db/archive.iosql', tables=['bars']
    )
    """
    with Database(source_uri) as source:
        # 先查看表名、行数和批次估算；dry-run 不会创建目标库。
        preview = source.migrate_to(
            target_uri,
            tables=tables,
            specs=specs,
            dry_run=True,
            verify='row-count',
            batch_size=2,
        )
        print('迁移预览:', preview.to_dict())

        # 迁移会复制持久化表契约和数据；row-count 会检查源、目标行数。
        result = source.migrate_to(
            target_uri,
            tables=tables,
            specs=specs,
            verify='row-count',
            batch_size=2,
        )
        assert result.verified
        print('迁移结果:', result.to_dict())
        return result


def run():
    # 每次运行使用独立目录，保证目标为空并保留已有运行结果。
    RUNTIME.mkdir(parents=True, exist_ok=True)

    sqlite_uri = f'sqlite://{SQLITE_PATH}'
    parquet_uri = f'parquet://{PARQUET_PATH}'

    # 准备一张独立的 SQLite 示例表，避免示例依赖外部数据。
    with Database(sqlite_uri) as source:
        bars = source.timeseries(
            'bars',
            time='ts',
            pk=['symbol', 'ts'],
            columns={'symbol': 'text', 'ts': 'integer', 'close': 'float'},
            partition_every='1day',
        )
        bars.write(SOURCE_ROWS)
        print('SQLite 源行数:', bars.count_rows())

    # 在同一次迁移中改为小时分区，源库仍保留天分区。
    result = migrate_sqlite_to_parquet(
        sqlite_uri, parquet_uri,
        specs={'bars': {'partition': 'duration(ts,every=1hour)'}},
    )
    assert result.verified

    # 以 Parquet URI 重开目标库，确认目标引擎能正常查询和读取文件。
    with Database(parquet_uri) as archive:
        migrated_bars = archive.table('bars')
        aapl_rows = migrated_bars.query(symbols='AAPL', order_by='ts').to_list()
        assert migrated_bars.count_rows() == len(SOURCE_ROWS)
        assert [row['close'] for row in aapl_rows] == [187.0, 188.0, 189.0]
        assert migrated_bars.validate(deep=True).ok

        print('Parquet 中 AAPL 行:', aapl_rows)
        print('Parquet 数据文件:', [item['path'] for item in migrated_bars.segments()])

    print('SQLite → Parquet 迁移和目标校验完成')
    print('演示数据目录:', RUNTIME)


if __name__ == '__main__':
    run()
