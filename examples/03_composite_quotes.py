"""Example: composite primary keys with duration partitions to power leaderboards."""

from pathlib import Path
import random
import shutil

from iosql import Database
from iosql.partition import DurationPartition

DB_URI = 'sqlite://examples/runtime/partition/composite_quotes.sqlite3'
DB_PATH = Path('examples/runtime/partition/composite_quotes.sqlite3')


def run():
    if DB_PATH.exists():
        shutil.rmtree(DB_PATH)

    db = Database(DB_URI)
    partition = DurationPartition(interval_ms='1day', ts_col='trade_ts')

    table = db['quotes']
    table.partition = partition

    seed_ts = 1705276800000
    data = []
    for layer in range(4):
        for minute in range(60):
            data.append({
                'trade_ts': seed_ts + layer * 3600000 + minute * 60000,
                'symbol': f'SYM{layer}',
                'venue': 'A',
                'price': 100 + layer * 2 + random.random(),
            })

    table.insert_all(data, pk=['trade_ts', 'symbol', 'venue'])

    summary = {}
    for row in table.select_all('*', where=None):
        key = row['symbol']
        summary[key] = max(summary.get(key, 0), row['price'])

    print('Partitions:', table.table_parts())
    print('Daily high per symbol:', summary)

    db.close()


if __name__ == '__main__':
    run()
