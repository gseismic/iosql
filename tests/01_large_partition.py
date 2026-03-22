"""High-volume duration partition integration test."""

from pathlib import Path

import arrow

from iosql import Database
from iosql.partition import DurationPartition


TOTAL_ROWS = 50000
BATCH_SIZE = 5000
DURATION_INTERVAL = '1day'


def _build_timestamp(base, offset_minutes):
    return int((base.shift(minutes=offset_minutes).timestamp()) * 1000)


def test_large_volume_duration_partition(tmp_path: Path):
    db_file = tmp_path / 'large_volume.sqlite3'
    db = Database(f'sqlite://{db_file}')
    partition = DurationPartition(interval_ms=DURATION_INTERVAL, ts_col='timestamp')

    base = arrow.get('2025-01-01T00:00:00+00:00')
    rows_inserted = 0

    while rows_inserted < TOTAL_ROWS:
        batch_rows = []
        for i in range(BATCH_SIZE):
            idx = rows_inserted + i
            if idx >= TOTAL_ROWS:
                break
            timestamp = _build_timestamp(base, idx)
            batch_rows.append({
                'timestamp': timestamp,
                'symbol': f'SYM{idx % 10}',
                'value': (idx % 100) * 0.5,
            })
        db['ingest'].insert_all(batch_rows, pk=['timestamp', 'symbol'], partition=partition)
        rows_inserted += len(batch_rows)

    assert rows_inserted == TOTAL_ROWS

    total_rows = db['ingest'].select_all('*', where=None)
    assert len(total_rows) == TOTAL_ROWS

    partitions = db['ingest'].table_parts()
    assert len(partitions) >= 3

    window_start = _build_timestamp(base, TOTAL_ROWS - 5000)
    window_end = _build_timestamp(base, TOTAL_ROWS - 1)
    window = db['ingest'].select(
        '*',
        where={
            'field': 'timestamp',
            'op': 'between_and',
            'value': [window_start, window_end],
        },
        to_dict=True,
    )

    assert len(window) > 0
    assert all(window_start <= row['timestamp'] <= window_end for row in window)

    db.close()
