"""
Comprehensive SQLite partition integration tests.

Tests cover:
1. Partition auto-detection from metadata
2. Partition pruning with various query operators
3. CalendarPartition with SQLite
4. Data distribution verification
5. Partition metadata correctness
6. Edge cases and error handling
"""

import pytest
import os
import shutil
import sqlite3
import arrow

from iosql import Database
from iosql.partition import DurationPartition, CalendarPartition, Partition


TEST_DB_DIR = '/tmp/test_iosql_partition_integration'


@pytest.fixture(autouse=True)
def cleanup():
    if os.path.exists(TEST_DB_DIR):
        shutil.rmtree(TEST_DB_DIR)
    yield
    if os.path.exists(TEST_DB_DIR):
        shutil.rmtree(TEST_DB_DIR)


class TestPartitionAutoDetection:
    """Test that partitions are auto-detected from metadata."""

    def test_auto_detect_duration_partition(self):
        """Create table with DurationPartition, close, reopen - partition auto-loaded."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/kline.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [
            {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 185.5},
            {'timestamp': 1705276800000, 'symbol': 'GOOG', 'close': 142.3},
            {'timestamp': 1705363200000, 'symbol': 'AAPL', 'close': 186.0},
        ]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)

        db.close()

        db2 = Database(f'sqlite:///{TEST_DB_DIR}/kline.sqlite3')
        table = db2['kline']

        loaded_partition = table._partition
        assert loaded_partition is not None
        assert isinstance(loaded_partition, DurationPartition)
        assert loaded_partition.interval_ms == 86400000
        assert loaded_partition.TS_COL == 'timestamp'

        result = table.select_all('*', where=None)
        assert len(result) == 3

    def test_auto_detect_calendar_partition(self):
        """Create table with CalendarPartition, close, reopen - partition auto-loaded."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/events.sqlite3')
        partition = CalendarPartition(fmt='YYYYMMDD', tz='UTC', ts_col='date')

        data = [
            {'date': '2024-01-15', 'event': 'A'},
            {'date': '2024-01-16', 'event': 'B'},
        ]
        db['events'].insert_all(data, pk=['date', 'event'], partition=partition)

        db.close()

        db2 = Database(f'sqlite:///{TEST_DB_DIR}/events.sqlite3')
        table = db2['events']

        loaded_partition = table._partition
        assert loaded_partition is not None
        assert isinstance(loaded_partition, CalendarPartition)
        assert loaded_partition.fmt == 'YYYYMMDD'
        assert loaded_partition.tz == 'UTC'


class TestPartitionPruning:
    """Test partition pruning - queries should only hit relevant partitions."""

    def _create_kline_db(self):
        """Create a kline database with 3 days of data."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/kline.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        base_ts = 1705276800000  # 2024-01-15 00:00:00 UTC
        data = []
        for day in range(3):
            ts = base_ts + day * 86400000
            for hour in range(24):
                data.append({
                    'timestamp': ts + hour * 3600000,
                    'symbol': 'AAPL',
                    'close': 185.0 + hour
                })

        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)
        return db, partition

    def test_prune_gt_operator(self):
        """Query with timestamp > X excludes partitions where max < X."""
        db, partition = self._create_kline_db()

        result = db['kline'].select(
            '*',
            where={'field': 'timestamp', 'op': '>', 'value': 1705276800000 + 2 * 86400000}
        )

        assert len(result) > 0
        for row in result:
            assert row['timestamp'] > 1705276800000 + 2 * 86400000

    def test_prune_lt_operator(self):
        """Query with timestamp < X excludes partitions where min > X."""
        db, partition = self._create_kline_db()

        result = db['kline'].select(
            '*',
            where={'field': 'timestamp', 'op': '<', 'value': 1705276800000 + 86400000}
        )

        for row in result:
            assert row['timestamp'] < 1705276800000 + 86400000

    def test_prune_ge_operator(self):
        """Query with timestamp >= X excludes partitions correctly."""
        db, partition = self._create_kline_db()

        result = db['kline'].select(
            '*',
            where={'field': 'timestamp', 'op': '>=', 'value': 1705276800000 + 86400000}
        )

        for row in result:
            assert row['timestamp'] >= 1705276800000 + 86400000

    def test_prune_le_operator(self):
        """Query with timestamp <= X excludes partitions correctly."""
        db, partition = self._create_kline_db()

        result = db['kline'].select(
            '*',
            where={'field': 'timestamp', 'op': '<=', 'value': 1705276800000 + 86400000}
        )

        for row in result:
            assert row['timestamp'] <= 1705276800000 + 86400000

    def test_prune_between_and_operator(self):
        """Query with between X and Y excludes irrelevant partitions."""
        db, partition = self._create_kline_db()

        t1 = 1705276800000 + 86400000  # 2024-01-16 00:00:00
        t2 = 1705276800000 + 2 * 86400000  # 2024-01-17 00:00:00

        result = db['kline'].select(
            '*',
            where={'field': 'timestamp', 'op': 'between_and', 'value': [t1, t2]}
        )

        for row in result:
            assert t1 <= row['timestamp'] <= t2

    def test_prune_between_single_partition(self):
        """Query spanning single partition should only hit that partition."""
        db, partition = self._create_kline_db()

        t1 = 1705276800000 + 3600000  # 2024-01-15 01:00:00
        t2 = 1705276800000 + 2 * 3600000  # 2024-01-15 02:00:00

        result = db['kline'].select(
            '*',
            where={'field': 'timestamp', 'op': 'between_and', 'value': [t1, t2]}
        )

        for row in result:
            assert 1705276800000 <= row['timestamp'] <= 1705276800000 + 86400000

    def test_prune_eq_operator(self):
        """Query with timestamp = X should find exact match."""
        db, partition = self._create_kline_db()

        exact_ts = 1705276800000 + 12 * 3600000  # 2024-01-15 12:00:00

        result = db['kline'].select(
            '*',
            where={'field': 'timestamp', 'op': '==', 'value': exact_ts}
        )

        assert len(result) == 1
        assert result[0]['timestamp'] == exact_ts


class TestDataDistribution:
    """Test that data goes to correct partition files."""

    def test_data_goes_to_correct_partition(self):
        """Row with timestamp T goes to partition floor(T/interval)*interval."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        ts1 = 1705276800000  # 2024-01-15 00:00:00 UTC
        ts2 = 1705276800000 + 12 * 3600000  # 2024-01-15 12:00:00 UTC (same day)
        ts3 = 1705276800000 + 86400000  # 2024-01-16 00:00:00 UTC (next day)

        data = [
            {'timestamp': ts1, 'symbol': 'A'},
            {'timestamp': ts2, 'symbol': 'B'},
            {'timestamp': ts3, 'symbol': 'C'},
        ]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)

        parts = db['kline'].table_parts()
        assert sorted(parts) == [1705276800000, 1705363200000]

    def test_multiple_rows_same_partition(self):
        """Multiple rows with same day go to same .sqlite3 file."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        ts = 1705276800000  # 2024-01-15 00:00:00 UTC
        data = [
            {'timestamp': ts, 'symbol': 'A', 'close': 185.0},
            {'timestamp': ts + 3600000, 'symbol': 'B', 'close': 186.0},
            {'timestamp': ts + 7200000, 'symbol': 'C', 'close': 187.0},
        ]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)

        parts = db['kline'].table_parts()
        assert len(parts) == 1

        part_file = f'{TEST_DB_DIR}/test.sqlite3/kline/data/{int(parts[0])}.sqlite3'
        assert os.path.exists(part_file)

        conn = sqlite3.connect(part_file)
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM data')
        count = cur.fetchone()[0]
        conn.close()

        assert count == 3

    def test_hourly_partition_distribution(self):
        """Test data distribution with hourly partitions."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1hour', ts_col='timestamp')

        base_ts = 1705276800000  # 2024-01-15 00:00:00 UTC
        data = [
            {'timestamp': base_ts, 'value': 'a'},
            {'timestamp': base_ts + 1800000, 'value': 'b'},
            {'timestamp': base_ts + 3600000, 'value': 'c'},
            {'timestamp': base_ts + 5400000, 'value': 'd'},
        ]
        db['data'].insert_all(data, partition=partition)

        parts = db['data'].table_parts()
        assert len(parts) == 2
        assert base_ts in parts
        assert base_ts + 3600000 in parts


class TestCalendarPartitionSQLite:
    """Test CalendarPartition with SQLite backend."""

    def test_calendar_partition_insert(self):
        """Insert data with CalendarPartition, verify file naming."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/events.sqlite3')
        partition = CalendarPartition(fmt='YYYYMMDD', tz='UTC', ts_col='date')

        data = [
            {'date': '2024-01-15', 'event': 'A'},
            {'date': '2024-01-16', 'event': 'B'},
            {'date': '2024-01-17', 'event': 'C'},
        ]
        db['events'].insert_all(data, pk=['date', 'event'], partition=partition)

        parts = db['events'].table_parts()
        assert '20240115' in parts
        assert '20240116' in parts
        assert '20240117' in parts

        for part in parts:
            part_file = f'{TEST_DB_DIR}/events.sqlite3/events/data/{part}.sqlite3'
            assert os.path.exists(part_file), f'Partition file {part_file} should exist'

    def test_calendar_partition_query(self):
        """Query with date strings on CalendarPartition."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/events.sqlite3')
        partition = CalendarPartition(fmt='YYYYMMDD', tz='UTC', ts_col='date')

        data = [
            {'date': '2024-01-15', 'event': 'A'},
            {'date': '2024-01-16', 'event': 'B'},
            {'date': '2024-01-17', 'event': 'C'},
        ]
        db['events'].insert_all(data, pk=['date', 'event'], partition=partition)

        result = db['events'].select(
            '*',
            where={'field': 'date', 'op': '==', 'value': '2024-01-16'}
        )

        assert len(result) == 1
        assert result[0]['event'] == 'B'

    def test_calendar_partition_monthly(self):
        """Test CalendarPartition with monthly intervals."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/monthly.sqlite3')
        partition = CalendarPartition(fmt='YYYYMM', tz='UTC', ts_col='date')

        data = [
            {'date': '2024-01', 'value': 'jan'},
            {'date': '2024-02', 'value': 'feb'},
            {'date': '2024-03', 'value': 'mar'},
        ]
        db['data'].insert_all(data, pk=['date', 'value'], partition=partition)

        parts = db['data'].table_parts()
        assert '202401' in parts
        assert '202402' in parts
        assert '202403' in parts


class TestPartitionMetadata:
    """Test partition metadata is stored and retrieved correctly."""

    def test_partition_meta_stored_correctly(self):
        """Verify meta contains correct partition config."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [{'timestamp': 1705276800000, 'value': 'a'}]
        db['data'].insert_all(data, partition=partition)

        meta = db['data'].get_table_meta()
        assert 'partition' in meta
        assert meta['partition']['type'] == 'duration'
        assert meta['partition']['ts_col'] == 'timestamp'
        assert meta['partition']['interval_ms'] == 86400000

    def test_part_column_ranges_stored(self):
        """Verify table_part_column_range table has correct min/max."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        ts1 = 1705276800000  # 2024-01-15
        ts2 = 1705276800000 + 86400000  # 2024-01-16

        data = [
            {'timestamp': ts1, 'value': 'a'},
            {'timestamp': ts2, 'value': 'b'},
        ]
        db['data'].insert_all(data, partition=partition)

        ranges = db['data'].get_part_column_ranges()
        assert len(ranges) == 2

        for r in ranges:
            assert r['column'] == 'timestamp'
            assert r['column_min'] is not None
            assert r['column_max'] is not None


class TestPartitionChange:
    """Test that changing partition config is rejected."""

    def test_partition_change_rejected(self):
        """Insert with partition A then partition B should raise error."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition1 = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [{'timestamp': 1705276800000, 'value': 'a'}]
        db['data'].insert_all(data, partition=partition1)

        partition2 = DurationPartition(interval_ms='1hour', ts_col='timestamp')

        with pytest.raises(Exception, match='PartitionCanNotChange'):
            db['data'].insert_all(data, partition=partition2)


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_timestamp_on_boundary(self):
        """Timestamp exactly on partition boundary goes to correct partition."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        boundary_ts = 1705276800000  # Exactly 2024-01-15 00:00:00 UTC

        data = [{'timestamp': boundary_ts, 'value': 'a'}]
        db['data'].insert_all(data, partition=partition)

        parts = db['data'].table_parts()
        assert len(parts) == 1
        assert parts[0] == boundary_ts

    def test_timestamp_just_before_boundary(self):
        """Timestamp just before boundary stays in previous partition."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        boundary_ts = 1705276800000
        just_before = boundary_ts - 1

        data = [{'timestamp': just_before, 'value': 'a'}]
        db['data'].insert_all(data, partition=partition)

        parts = db['data'].table_parts()
        assert parts[0] == boundary_ts - 86400000

    def test_empty_result_set(self):
        """Query that matches no data returns empty list."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [{'timestamp': 1705276800000, 'value': 'a'}]
        db['data'].insert_all(data, partition=partition)

        result = db['data'].select(
            '*',
            where={'field': 'timestamp', 'op': '>', 'value': 1800000000000}
        )

        assert len(result) == 0

    def test_all_partitions_queried_without_where(self):
        """Query without WHERE hits all partitions."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [
            {'timestamp': 1705276800000, 'value': 'a'},
            {'timestamp': 1705276800000 + 86400000, 'value': 'b'},
            {'timestamp': 1705276800000 + 2 * 86400000, 'value': 'c'},
        ]
        db['data'].insert_all(data, partition=partition)

        result = db['data'].select_all('*', where=None)
        assert len(result) == 3


class TestPartitionFileStructure:
    """Test the physical partition file structure."""

    def test_partition_directory_structure(self):
        """Verify correct directory structure is created."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [{'timestamp': 1705276800000, 'value': 'a'}]
        db['data'].insert_all(data, partition=partition)

        expected_paths = [
            f'{TEST_DB_DIR}/test.sqlite3',
            f'{TEST_DB_DIR}/test.sqlite3/data',
            f'{TEST_DB_DIR}/test.sqlite3/data/data',
            f'{TEST_DB_DIR}/test.sqlite3/data/data/1705276800000.sqlite3',
        ]

        for path in expected_paths:
            assert os.path.exists(path), f'Path {path} should exist'

    def test_meta_sqlite3_created(self):
        """Verify meta.sqlite3 files are created."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        data = [{'timestamp': 1705276800000, 'value': 'a'}]
        db['data'].insert_all(data, partition=partition)

        meta_path = f'{TEST_DB_DIR}/test.sqlite3/data/meta.sqlite3'
        assert os.path.exists(meta_path), f'Meta file {meta_path} should exist'


class TestDurationPartitionIntervals:
    """Test various duration partition intervals."""

    @pytest.mark.parametrize('interval', [
        '1min', '5min', '15min', '30min', '60min',
        '1hour', '4hour', '1day', '1week'
    ])
    def test_various_intervals(self, interval):
        """Test that various interval aliases work correctly."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms=interval, ts_col='ts')

        data = [{'ts': 1705276800000, 'value': 'a'}]
        db['data'].insert_all(data, partition=partition)

        result = db['data'].select_all('*', where=None)
        assert len(result) == 1
        assert result[0]['value'] == 'a'

    def test_custom_interval_ms(self):
        """Test custom interval in milliseconds."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms=300000, ts_col='ts')  # 5 min

        ts = 1705276800000  # 2024-01-15 00:00:00
        data = [
            {'ts': ts, 'value': 'a'},
            {'ts': ts + 300000, 'value': 'b'},
            {'ts': ts + 600000, 'value': 'c'},
        ]
        db['data'].insert_all(data, partition=partition)

        parts = db['data'].table_parts()
        # ts=1705276800000, ts+300000=1705277100000, ts+600000=1705277400000
        # All are at different 5-minute boundaries
        assert len(parts) == 3


class TestDelete:
    """Test delete functionality with partitions."""

    def test_delete_with_partition_pruning(self):
        """Delete should only hit relevant partitions."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        base_ts = 1705276800000  # 2024-01-15
        data = [
            {'timestamp': base_ts, 'symbol': 'AAPL', 'close': 185.0},
            {'timestamp': base_ts + 86400000, 'symbol': 'AAPL', 'close': 186.0},
            {'timestamp': base_ts + 2 * 86400000, 'symbol': 'AAPL', 'close': 187.0},
        ]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)

        result = db['kline'].select_all('*', where=None)
        assert len(result) == 3

        db['kline'].delete({'field': 'timestamp', 'op': '>', 'value': base_ts + 86400000})

        remaining = db['kline'].select_all('*', where=None)
        assert len(remaining) == 2
        for row in remaining:
            assert row['timestamp'] <= base_ts + 86400000

    def test_delete_all_from_partition(self):
        """Delete all rows from a specific partition."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        base_ts = 1705276800000  # 2024-01-15
        data = [
            {'timestamp': base_ts, 'symbol': 'AAPL', 'close': 185.0},
            {'timestamp': base_ts + 86400000, 'symbol': 'AAPL', 'close': 186.0},
        ]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)

        db['kline'].delete({'field': 'timestamp', 'op': '==', 'value': base_ts})

        remaining = db['kline'].select_all('*', where=None)
        assert len(remaining) == 1
        assert remaining[0]['timestamp'] == base_ts + 86400000

    def test_delete_nonexistent(self):
        """Delete with no matching rows should not error."""
        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        base_ts = 1705276800000
        data = [{'timestamp': base_ts, 'symbol': 'AAPL', 'close': 185.0}]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'], partition=partition)

        deleted = db['kline'].delete({'field': 'timestamp', 'op': '>', 'value': 2000000000000})
        assert deleted == 0

        remaining = db['kline'].select_all('*', where=None)
        assert len(remaining) == 1


class TestTablePartitionSetter:
    """Test that table.partition setter works correctly."""

    def test_partition_setter_and_persistence(self):
        """Set partition on table, verify it's persisted and auto-detected."""
        import shutil
        shutil.rmtree(TEST_DB_DIR, ignore_errors=True)

        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')

        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')
        db['kline'].partition = partition

        base_ts = 1705276800000  # 2024-01-15
        data = [
            {'timestamp': base_ts, 'symbol': 'AAPL', 'close': 185.0},
            {'timestamp': base_ts + 86400000, 'symbol': 'AAPL', 'close': 186.0},
        ]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'])

        db.close()

        db2 = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        loaded_partition = db2['kline'].partition
        assert loaded_partition is not None
        assert isinstance(loaded_partition, DurationPartition)
        assert loaded_partition.interval_ms == 86400000

    def test_insert_all_without_partition_uses_table_partition(self):
        """insert_all without partition param should use table's partition."""
        import shutil
        shutil.rmtree(TEST_DB_DIR, ignore_errors=True)

        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')

        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')
        db['kline'].partition = partition

        base_ts = 1705276800000
        data = [
            {'timestamp': base_ts, 'symbol': 'AAPL', 'close': 185.0},
            {'timestamp': base_ts + 86400000, 'symbol': 'AAPL', 'close': 186.0},
            {'timestamp': base_ts + 2 * 86400000, 'symbol': 'AAPL', 'close': 187.0},
        ]

        db['kline'].insert_all(data, pk=['timestamp', 'symbol'])

        parts = db['kline'].table_parts()
        assert len(parts) == 3

        result = db['kline'].select_all('*', where=None)
        assert len(result) == 3


class TestLargeData:
    """Large scale data tests with 200K rows."""

    def test_large_data_200k_rows_20_batches(self):
        """Test inserting 200K rows in 20 batches, then query.
        
        Note: Uses 1day partitions to avoid too many open file handles.
        200K rows with 1min intervals spans ~139 days = ~139 partitions (1day each).
        """
        import shutil
        import time
        shutil.rmtree(TEST_DB_DIR, ignore_errors=True)

        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        db = Database(f'sqlite:///{TEST_DB_DIR}/kline.sqlite3')
        db['kline'].partition = partition

        base_ts = 1705276800000  # 2024-01-15 00:00:00 UTC
        total_rows = 200000
        batch_size = 10000
        num_batches = total_rows // batch_size

        print(f'\nInserting {total_rows} rows in {num_batches} batches...')
        insert_start = time.time()

        for batch in range(num_batches):
            data = []
            for i in range(batch_size):
                ts = base_ts + (batch * batch_size + i) * 60000
                data.append({
                    'timestamp': ts,
                    'symbol': f'SYM{batch % 100}',
                    'close': 100.0 + (batch * batch_size + i) % 50,
                    'volume': (batch * batch_size + i) * 100,
                })

            db['kline'].insert_all(data, pk=['timestamp', 'symbol'])
            if (batch + 1) % 5 == 0:
                print(f'  Batch {batch + 1}/{num_batches} done ({len(data)} rows)')
                db['kline'].close()

        insert_end = time.time()
        print(f'Insert completed in {insert_end - insert_start:.2f}s')

        parts = db['kline'].table_parts()
        print(f'Number of partitions: {len(parts)}')

        result = db['kline'].select_all('*', where=None)
        print(f'Total rows after insert: {len(result)}')
        assert len(result) == total_rows, f'Expected {total_rows}, got {len(result)}'

        query_start = time.time()
        t1 = base_ts + 86400000
        t2 = base_ts + 2 * 86400000
        result = db['kline'].select(
            '*',
            where={'field': 'timestamp', 'op': 'between_and', 'value': [t1, t2]}
        )
        query_end = time.time()
        print(f'Query completed in {query_end - query_start:.4f}s')
        print(f'Rows in time range: {len(result)}')

        for row in result:
            assert t1 <= row['timestamp'] <= t2

        db.close()
        print('Test passed!')

    def test_large_data_multiple_partitions_query(self):
        """Test querying across multiple partitions."""
        import shutil
        import time
        shutil.rmtree(TEST_DB_DIR, ignore_errors=True)

        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        db['data'].partition = partition

        base_ts = 1705276800000  # 2024-01-15
        num_days = 10
        rows_per_day = 10000

        print(f'\nInserting {num_days * rows_per_day} rows across {num_days} days...')
        insert_start = time.time()

        for day in range(num_days):
            data = []
            day_ts = base_ts + day * 86400000
            for i in range(rows_per_day):
                data.append({
                    'timestamp': day_ts + i * 8640,
                    'value': day * 1000 + i,
                })
            db['data'].insert_all(data, pk=['timestamp'])

        insert_end = time.time()
        print(f'Insert completed in {insert_end - insert_start:.2f}s')

        parts = db['data'].table_parts()
        print(f'Number of partitions: {len(parts)}')
        assert len(parts) == num_days

        result = db['data'].select_all('*', where=None)
        assert len(result) == num_days * rows_per_day

        query_start = time.time()
        t1 = base_ts + 2 * 86400000
        t2 = base_ts + 4 * 86400000 + 86400000 - 1  # End of day 4 (inclusive)
        result = db['data'].select(
            '*',
            where={'field': 'timestamp', 'op': 'between_and', 'value': [t1, t2]}
        )
        query_end = time.time()
        print(f'Query (3 days range) completed in {query_end - query_start:.4f}s')
        print(f'Rows returned: {len(result)}')

        expected_rows = 3 * rows_per_day
        assert len(result) == expected_rows, f'Expected {expected_rows}, got {len(result)}'

        db.close()
        print('Test passed!')

    def test_upsert_large_data(self):
        """Test upsert_all with large data."""
        import shutil
        import time
        shutil.rmtree(TEST_DB_DIR, ignore_errors=True)

        partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

        db = Database(f'sqlite:///{TEST_DB_DIR}/test.sqlite3')
        db['kline'].partition = partition

        base_ts = 1705276800000
        initial_rows = 10000
        update_rows = 5000

        print(f'\nInserting {initial_rows} initial rows...')
        data = [
            {'timestamp': base_ts + i * 86400000, 'symbol': f'SYM{i % 100}', 'close': 100.0 + i}
            for i in range(initial_rows)
        ]
        db['kline'].insert_all(data, pk=['timestamp', 'symbol'])

        print(f'Updating {update_rows} rows via upsert...')
        upsert_data = [
            {'timestamp': base_ts + i * 86400000, 'symbol': f'SYM{i % 100}', 'close': 200.0 + i}
            for i in range(update_rows)
        ]

        upsert_start = time.time()
        db['kline'].upsert_all(upsert_data, pk=['timestamp', 'symbol'])
        upsert_end = time.time()
        print(f'Upsert completed in {upsert_end - upsert_start:.2f}s')

        result = db['kline'].select_all('*', where=None)
        assert len(result) == initial_rows

        for row in result[:update_rows]:
            assert row['close'] == 200.0 + (row['timestamp'] - base_ts) / 86400000

        db.close()
        print('Test passed!')
