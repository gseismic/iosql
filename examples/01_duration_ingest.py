"""Example: ingest minute-level metrics into daily partitions."""

from pathlib import Path
import shutil

import arrow
from iosql import Database
from iosql.partition import DurationPartition

DB_URI = 'sqlite://examples/runtime/partition/duration_metrics.sqlite3'
DB_PATH = Path('examples/runtime/partition/duration_metrics.sqlite3')


def run():
    if DB_PATH.exists():
        shutil.rmtree(DB_PATH)

    db = Database(DB_URI)
    partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

    start = arrow.get('2025-01-01T00:00:00+00:00')
    rows = []
    for minute in range(60 * 24 * 2):  # two days of data
        timestamp = (start.shift(minutes=minute).timestamp()) * 1000
        rows.append({
            'timestamp': int(timestamp),
            'symbol': 'METRIC_A',
            'value': minute % 50 + 100,
        })

    db['metrics'].insert_all(rows, pk=['timestamp', 'symbol'], partition=partition)

    print('Partitions created:', db['metrics'].table_parts())

    window_start = arrow.get('2025-01-02T03:00:00+00:00').timestamp() * 1000
    window_end = arrow.get('2025-01-02T04:00:00+00:00').timestamp() * 1000
    window_rows = db['metrics'].select(
        '*',
        where={'field': 'timestamp', 'op': 'between_and', 'value': [int(window_start), int(window_end)]},
    )

    print('Rows in 1-hour window:', len(window_rows))
    print('Sample row:', dict(window_rows[0]))

    db.close()


if __name__ == '__main__':
    run()
