import pytest
import os
import shutil
from iosql.partition import DurationPartition, CalendarPartition, Partition
from iosql import Database


class TestDurationPartition:
    def test_basic_creation(self):
        p = DurationPartition(interval_ms=86400000, ts_col='timestamp')
        assert p.interval_ms == 86400000
        assert p.TS_COL == 'timestamp'
        assert p.PARTITION_TYPE == 'duration'

    def test_interval_alias_1day(self):
        p = DurationPartition(interval_ms='1day', ts_col='ts')
        assert p.interval_ms == 86400000

    def test_interval_alias_1hour(self):
        p = DurationPartition(interval_ms='1hour', ts_col='ts')
        assert p.interval_ms == 3600000

    def test_interval_alias_5min(self):
        p = DurationPartition(interval_ms='5min', ts_col='ts')
        assert p.interval_ms == 300000

    def test_get_key(self):
        p = DurationPartition(interval_ms=86400000, ts_col='timestamp')
        row = {'timestamp': 1705276800000}  # 2024-01-15 00:00:00 UTC
        key = p.get_key(row)
        assert key == 1705276800000

    def test_get_key_middle_of_day(self):
        p = DurationPartition(interval_ms=86400000, ts_col='timestamp')
        row = {'timestamp': 1705276800000 + 3600000}  # 2024-01-15 01:00:00 UTC
        key = p.get_key(row)
        assert key == 1705276800000  # Still aligned to UTC midnight

    def test_get_range(self):
        p = DurationPartition(interval_ms=86400000, ts_col='timestamp')
        key = 1705276800000
        start, end = p.get_range(key)
        assert start == 1705276800000
        assert end == 1705363200000

    def test_get_file_suffix(self):
        p = DurationPartition(interval_ms=86400000, ts_col='timestamp')
        suffix = p.get_file_suffix(1705276800000)
        assert suffix == '1705276800000.sqlite3'

    def test_to_meta(self):
        p = DurationPartition(interval_ms=86400000, ts_col='timestamp')
        meta = p.to_meta()
        assert meta == {
            'type': 'duration',
            'ts_col': 'timestamp',
            'interval_ms': 86400000
        }

    def test_from_meta(self):
        meta = {
            'type': 'duration',
            'ts_col': 'timestamp',
            'interval_ms': 86400000
        }
        p = DurationPartition.from_meta(meta)
        assert p.interval_ms == 86400000
        assert p.TS_COL == 'timestamp'

    def test_all_interval_aliases(self):
        aliases = {
            '1min': 60_000,
            '5min': 300_000,
            '15min': 900_000,
            '30min': 1_800_000,
            '60min': 3_600_000,
            '1hour': 3_600_000,
            '4hour': 14_400_000,
            '1day': 86_400_000,
            '1week': 604_800_000,
            '1month': 2_592_000_000,
            '1year': 31_536_000_000,
        }
        for alias, expected in aliases.items():
            p = DurationPartition(interval_ms=alias, ts_col='ts')
            assert p.interval_ms == expected, f'Failed for alias {alias}'


class TestCalendarPartition:
    def test_basic_creation(self):
        p = CalendarPartition(fmt='YYYYMMDD', tz='Asia/Shanghai', ts_col='date')
        assert p.fmt == 'YYYYMMDD'
        assert p.tz == 'Asia/Shanghai'
        assert p.TS_COL == 'date'
        assert p.PARTITION_TYPE == 'calendar'

    def test_get_key_shanghai(self):
        p = CalendarPartition(fmt='YYYYMMDD', tz='Asia/Shanghai', ts_col='date')
        row = {'date': '2024-01-15 08:30:00+08:00'}
        key = p.get_key(row)
        assert key == '20240115'

    def test_get_key_utc(self):
        p = CalendarPartition(fmt='YYYYMMDD', tz='UTC', ts_col='date')
        row = {'date': '2024-01-15 00:30:00+00:00'}
        key = p.get_key(row)
        assert key == '20240115'

    def test_get_key_year_month(self):
        p = CalendarPartition(fmt='YYYYMM', tz='UTC', ts_col='date')
        row = {'date': '2024-01-15 00:00:00+00:00'}
        key = p.get_key(row)
        assert key == '202401'

    def test_get_range(self):
        p = CalendarPartition(fmt='YYYYMMDD', tz='UTC', ts_col='date')
        key = '20240115'
        start, end = p.get_range(key)
        # 2024-01-15 00:00:00 UTC to 2024-01-16 00:00:00 UTC
        assert start == 1705276800000
        assert end == 1705363200000

    def test_get_file_suffix(self):
        p = CalendarPartition(fmt='YYYYMMDD', tz='UTC', ts_col='date')
        suffix = p.get_file_suffix('20240115')
        assert suffix == '20240115.sqlite3'

    def test_to_meta(self):
        p = CalendarPartition(fmt='YYYYMMDD', tz='Asia/Shanghai', ts_col='date')
        meta = p.to_meta()
        assert meta == {
            'type': 'calendar',
            'ts_col': 'date',
            'fmt': 'YYYYMMDD',
            'tz': 'Asia/Shanghai'
        }

    def test_from_meta(self):
        meta = {
            'type': 'calendar',
            'ts_col': 'date',
            'fmt': 'YYYYMMDD',
            'tz': 'Asia/Shanghai'
        }
        p = CalendarPartition.from_meta(meta)
        assert p.fmt == 'YYYYMMDD'
        assert p.tz == 'Asia/Shanghai'
        assert p.TS_COL == 'date'


class TestPartitionFromMeta:
    def test_from_meta_duration(self):
        meta = {
            'type': 'duration',
            'ts_col': 'timestamp',
            'interval_ms': 86400000
        }
        p = Partition.from_meta(meta)
        assert isinstance(p, DurationPartition)
        assert p.interval_ms == 86400000

    def test_from_meta_calendar(self):
        meta = {
            'type': 'calendar',
            'ts_col': 'date',
            'fmt': 'YYYYMMDD',
            'tz': 'Asia/Shanghai'
        }
        p = Partition.from_meta(meta)
        assert isinstance(p, CalendarPartition)
        assert p.fmt == 'YYYYMMDD'

    def test_from_meta_unknown_type(self):
        meta = {'type': 'unknown', 'ts_col': 'date'}
        with pytest.raises(ValueError, match='Unknown partition type'):
            Partition.from_meta(meta)


TEST_DB_DIR = '/tmp/test_partition_db'

@pytest.fixture(autouse=True)
def cleanup():
    if os.path.exists(TEST_DB_DIR):
        shutil.rmtree(TEST_DB_DIR)
    yield
    if os.path.exists(TEST_DB_DIR):
        shutil.rmtree(TEST_DB_DIR)


class TestSQLiteWithPartition:
    def test_insert_with_duration_partition(self):
        db = Database(f'sqlite3:///{TEST_DB_DIR}/kline.sqlite3')
        partition = DurationPartition(interval_ms=86400000, ts_col='timestamp')

        data = [
            {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 185.5},
            {'timestamp': 1705276800000, 'symbol': 'GOOG', 'close': 142.3},
            {'timestamp': 1705363200000, 'symbol': 'AAPL', 'close': 186.0},
        ]

        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)

        assert os.path.exists(f'{TEST_DB_DIR}/kline.sqlite3/kline/data/1705276800000.sqlite3')
        assert os.path.exists(f'{TEST_DB_DIR}/kline.sqlite3/kline/data/1705363200000.sqlite3')

    def test_select_with_duration_partition(self):
        db = Database(f'sqlite3:///{TEST_DB_DIR}/kline.sqlite3')
        partition = DurationPartition(interval_ms=86400000, ts_col='timestamp')

        data = [
            {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 185.5},
            {'timestamp': 1705276800000, 'symbol': 'GOOG', 'close': 142.3},
            {'timestamp': 1705363200000, 'symbol': 'AAPL', 'close': 186.0},
        ]

        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)
        result = db['kline'].select_all('*', where=None)

        assert len(result) == 3

    def test_table_parts_after_insert(self):
        db = Database(f'sqlite3:///{TEST_DB_DIR}/kline.sqlite3')
        partition = DurationPartition(interval_ms=86400000, ts_col='timestamp')

        data = [
            {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 185.5},
            {'timestamp': 1705363200000, 'symbol': 'AAPL', 'close': 186.0},
        ]

        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)
        parts = db['kline'].table_parts()

        assert sorted(parts) == [1705276800000, 1705363200000]

    def test_duration_partition_alias(self):
        db = Database(f'sqlite3:///{TEST_DB_DIR}/kline.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [
            {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 185.5},
        ]

        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)
        result = db['kline'].select_all('*', where=None)

        assert len(result) == 1
        assert result[0]['symbol'] == 'AAPL'

    def test_hourly_partition(self):
        db = Database(f'sqlite3:///{TEST_DB_DIR}/kline.sqlite3')
        partition = DurationPartition(interval_ms='1hour', ts_col='timestamp')

        data = [
            {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 185.5},  # 2024-01-15 00:00:00
            {'timestamp': 1705276800000 + 3600000, 'symbol': 'GOOG', 'close': 142.3},  # 2024-01-15 01:00:00
        ]

        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)
        parts = db['kline'].table_parts()

        assert len(parts) == 2
        assert 1705276800000 in parts
        assert 1705280400000 in parts

    def test_composite_pk_with_partition(self):
        db = Database(f'sqlite3:///{TEST_DB_DIR}/order_items.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='created_at')

        data = [
            {'order_id': 1, 'product_id': 100, 'qty': 2, 'created_at': 1705276800000},
            {'order_id': 1, 'product_id': 101, 'qty': 1, 'created_at': 1705276800000},
            {'order_id': 2, 'product_id': 100, 'qty': 5, 'created_at': 1705363200000},
        ]

        db['order_items'].insert_all(data, pk=['order_id', 'product_id'], partition=partition)

        result = db['order_items'].select_all('*', where=None)
        assert len(result) == 3

    def test_replace_with_partition(self):
        db = Database(f'sqlite3:///{TEST_DB_DIR}/kline.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [
            {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 185.5},
        ]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)

        new_data = [
            {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 190.0},
        ]
        db['kline'].insert_all(new_data, pk=['timestamp', 'symbol'], partition=partition, replace=True)

        result = db['kline'].select_all('*', where=None)
        assert len(result) == 1
        assert result[0]['close'] == 190.0

    def test_multiple_intervals(self):
        intervals = ['1min', '5min', '15min', '30min', '60min', '1hour', '4hour', '1day', '1week']
        for i, interval in enumerate(intervals):
            db = Database(f'sqlite3:///{TEST_DB_DIR}/test_{i}.sqlite3')
            partition = DurationPartition(interval_ms=interval, ts_col='ts')

            data = [{'ts': 1705276800000, 'value': interval}]
            db['data'].insert_all(data, partition=partition)

            result = db['data'].select_all('*', where=None)
            assert len(result) == 1, f'Failed for interval {interval}'
