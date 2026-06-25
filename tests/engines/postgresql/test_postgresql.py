import os
import pytest
import psycopg2
from psycopg2 import sql

from iosql import Database, Partition, Segment
from iosql.partition import DurationPartition
from iosql.engines.postgresql import Table

POSTGRES_URI = os.environ.get('POSTGRES_DSN') or 'postgresql://postgres:@localhost:5432/test_db'
_POSTGRES_ERROR = None


def _postgres_available():
    global _POSTGRES_ERROR
    if not POSTGRES_URI:
        _POSTGRES_ERROR = 'POSTGRES_DSN not configured'
        return False
    try:
        with psycopg2.connect(POSTGRES_URI) as conn:
            conn.autocommit = True
        return True
    except psycopg2.Error as err:
        _POSTGRES_ERROR = str(err)
        return False


_POSTGRES_AVAILABLE = _postgres_available()
pytestmark = pytest.mark.skipif(
    not _POSTGRES_AVAILABLE,
    reason=(f'PostgreSQL instance not available: {_POSTGRES_ERROR}' if _POSTGRES_ERROR else 'PostgreSQL instance not available'),
)


def _drop_table(uri, table_name):
    with psycopg2.connect(uri) as conn:
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL('DROP TABLE IF EXISTS {} CASCADE').format(sql.Identifier(table_name))
            )


@pytest.fixture(scope='module')
def pg_table():
    table_name = 'iosql_test_postgresql'
    _drop_table(POSTGRES_URI, table_name)

    partition = DurationPartition(interval_ms='1day', ts_col='timestamp')
    table = Table(
        POSTGRES_URI,
        table_name,
        partition_mode=Table.PARTITION_MODE_NATIVE,
        partition=partition,
        verbose=0,
    )

    column_types = {
        'timestamp': 'BIGINT',
        'symbol': 'TEXT',
        'value': 'DOUBLE PRECISION',
    }

    table.create_table(column_types, pk=['timestamp', 'symbol'], partition=partition)

    yield table

    table.close()
    _drop_table(POSTGRES_URI, table_name)


def test_postgresql_partition_insert_and_query(pg_table):
    base_ts = 1705276800000

    data = [
        {'timestamp': base_ts + day * 86400000, 'symbol': f'SYM{day}', 'value': float(day)}
        for day in range(3)
    ]

    pg_table.insert_all(data, pk=['timestamp', 'symbol'], partition=pg_table.partition)

    all_rows = pg_table.select_all('*', where=None)
    assert len(all_rows) == len(data)

    # Query the second day's partition via WHERE clause
    day_two_start = base_ts + 1 * 86400000
    day_two_end = day_two_start + 86400000
    filtered = pg_table.select(
        '*',
        where=f'timestamp >= {day_two_start} AND timestamp < {day_two_end}',
    )

    assert len(filtered) == 1
    assert filtered[0]['symbol'] == 'SYM1'
    assert filtered[0]['value'] == pytest.approx(1.0)


def test_sqlite_to_postgresql_migration_verify_and_query(tmp_path):
    table_name = 'iosql_test_migration_ticks'
    _drop_table(POSTGRES_URI, table_name)
    source_uri = f"sqlite://{tmp_path}/source.iosql"

    try:
        with Database(source_uri) as source:
            ticks = source.table(
                table_name,
                pk=['symbol', 'ts'],
                columns={'symbol': 'text', 'ts': 'integer', 'price': 'float'},
                partition=Partition.duration('ts', every='1day'),
                segment=Segment.rows(max_rows=2, target_rows=2, min_rows=1),
                order_key='ts',
            )
            ticks.write([
                {'symbol': 'AAPL', 'ts': 1735689600000, 'price': 187.4},
                {'symbol': 'MSFT', 'ts': 1735689601000, 'price': 420.3},
                {'symbol': 'AAPL', 'ts': 1735776000000, 'price': 189.1},
                {'symbol': 'MSFT', 'ts': 1735776001000, 'price': 421.0},
            ])
            source_spec = ticks.spec()

            plan = source.to_postgresql(
                POSTGRES_URI,
                tables={table_name: table_name},
                dry_run=True,
                batch_size=2,
            )
            assert plan.dry_run is True
            assert plan.source_rows == 4
            assert plan.batches == 2

            result = source.to_postgresql(
                POSTGRES_URI,
                tables={table_name: table_name},
                verify='row-count',
                batch_size=2,
            )
            assert result.verified is True
            assert result.source_rows == 4
            assert result.target_rows == 4

        with Database(POSTGRES_URI) as target:
            names = target.table_names()
            assert table_name in names
            assert not any(
                name.startswith(f'{table_name}_p_')
                for name in names
            )

            target_table = target.table(table_name, spec=source_spec)
            assert target_table.count_rows() == 4
            rows = target_table.select(
                ['symbol', 'ts', 'price'],
                where={'field': 'symbol', 'op': '==', 'value': 'AAPL'},
                order_by='ts',
            )
            assert [row['price'] for row in rows] == [187.4, 189.1]

            roundtrip_uri = f"sqlite://{tmp_path}/roundtrip.iosql"
            roundtrip_result = target.migrate_to(
                roundtrip_uri,
                tables={table_name: 'ticks_from_postgresql'},
                verify='row-count',
                batch_size=2,
            )
            assert roundtrip_result.verified is True
            assert roundtrip_result.source_rows == 4
            assert roundtrip_result.target_rows == 4

        with Database(roundtrip_uri) as roundtrip:
            copied = roundtrip.table('ticks_from_postgresql')
            assert copied.count_rows() == 4
    finally:
        _drop_table(POSTGRES_URI, table_name)


def test_sqlite_calendar_text_migration_verify_and_query(tmp_path):
    table_name = 'iosql_test_migration_events'
    _drop_table(POSTGRES_URI, table_name)
    source_uri = f"sqlite://{tmp_path}/calendar_source.iosql"

    try:
        with Database(source_uri) as source:
            events = source.table(
                table_name,
                pk=['event_date', 'name'],
                columns={'event_date': 'text', 'name': 'text', 'city': 'text'},
                partition=Partition.calendar(
                    'event_date',
                    every='1day',
                    timezone='UTC',
                ),
                order_key='event_date',
            )
            events.write([
                {'event_date': '2026-01-05', 'name': 'open', 'city': 'Shanghai'},
                {'event_date': '2026-01-06', 'name': 'review', 'city': 'Beijing'},
                {'event_date': '2026-01-06', 'name': 'close', 'city': 'Shanghai'},
            ])

            result = source.to_postgresql(
                POSTGRES_URI,
                tables={table_name: table_name},
                verify='row-count',
                batch_size=2,
            )
            assert result.verified is True
            assert result.source_rows == 3
            assert result.target_rows == 3

        with Database(POSTGRES_URI) as target:
            table = target.table(table_name)
            rows = table.select(
                ['event_date', 'name'],
                where={'field': 'event_date', 'op': '==', 'value': '2026-01-06'},
                order_by='name',
            )
            assert [row['name'] for row in rows] == ['close', 'review']
    finally:
        _drop_table(POSTGRES_URI, table_name)
