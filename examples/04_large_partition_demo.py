"""Large-volume duration partition demo.

This script inserts 500K rows across 50 daily partitions and prints a short
summary of the resulting partition layout.
"""

from pathlib import Path
import shutil
import time

from iosql import Database
from iosql.partition import DurationPartition

DB_PATH = Path('examples/runtime/partition/large_partition_demo.sqlite3')
DB_URI = f'sqlite://{DB_PATH}'
TOTAL_DAYS = 50
BATCH_SIZE = 10_000
TOTAL_ROWS = TOTAL_DAYS * BATCH_SIZE


def run():
    if DB_PATH.exists():
        shutil.rmtree(DB_PATH)

    db = Database(DB_URI)
    table = db['ticks']
    partition = DurationPartition(interval_ms='1day', ts_col='timestamp')
    table.partition = partition

    base_ts = 1735689600000  # 2025-01-01 00:00:00 UTC

    start = time.time()
    for day in range(TOTAL_DAYS):
        batch = []
        day_start = base_ts + day * 86_400_000
        for i in range(BATCH_SIZE):
            batch.append({
                'timestamp': day_start + i * 8_640,
                'symbol': f'SYM{i % 100}',
                'price': 100.0 + (i % 100) * 0.1 + day,
                'volume': day * BATCH_SIZE + i,
            })
        table.insert_all(batch, pk=['timestamp', 'symbol'])
        if (day + 1) % 10 == 0:
            print(f'Inserted {day + 1}/{TOTAL_DAYS} partitions, {len(batch)} rows in last batch')

    elapsed = time.time() - start
    parts = table.table_parts()
    print(f'Inserted {TOTAL_ROWS} rows across {len(parts)} partitions in {elapsed:.2f}s')
    print('First 5 partitions:', parts[:5])

    sample_start = base_ts + 20 * 86_400_000
    sample_end = sample_start + 86_400_000 - 1
    sample = table.select(
        '*',
        where={'field': 'timestamp', 'op': 'between_and', 'value': [sample_start, sample_end]},
        to_dict=True,
    )
    print('Sample window rows:', len(sample))
    print('Sample row:', sample[0])

    db.close()


if __name__ == '__main__':
    run()
