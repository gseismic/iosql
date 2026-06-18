import os
import pytest
import psycopg2
from psycopg2 import sql

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
