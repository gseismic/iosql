# iosql

SQLite wrapper for time series data supporting datetime-based partitioning with millisecond precision.

## Features

- **Partition-based storage**: Data stored in separate SQLite files per partition
- **Partition pruning**: Queries automatically skip irrelevant partitions
- **Auto-detection**: Partition configuration loaded from table metadata
- **Multiple partition types**: Duration (1min, 1hour, 1day, etc.) and Calendar (YYYYMMDD, YYYYMM, etc.)
- **High performance**: Tested with 200K+ rows

## Quick Start

```python
from iosql import Database
from iosql.partition import DurationPartition

db = Database('sqlite://database/kline.sqlite3')

# Set partition on table (persisted to metadata)
db['kline'].partition = DurationPartition(interval_ms='1day', ts_col='timestamp')

# Insert data (partition auto-used from table)
data = [
    {'timestamp': 1705276800000, 'symbol': 'AAPL', 'close': 185.5},
    {'timestamp': 1705276800000 + 86400000, 'symbol': 'AAPL', 'close': 186.0},
]
db['kline'].insert_all(data, pk=['timestamp', 'symbol'])

# Query with partition pruning
result = db['kline'].select(
    '*',
    where={'field': 'timestamp', 'op': 'between_and', 
           'value': [1705276800000, 1705363200000]}
)
```

## Partition Types

### DurationPartition

Integer-based partitions with millisecond intervals.

```python
from iosql.partition import DurationPartition

# Using string aliases
partition = DurationPartition(interval_ms='1day', ts_col='timestamp')
partition = DurationPartition(interval_ms='1hour', ts_col='timestamp')
partition = DurationPartition(interval_ms='5min', ts_col='timestamp')

# Using milliseconds directly
partition = DurationPartition(interval_ms=3600000, ts_col='timestamp')  # 1 hour
```

Supported interval aliases: `1min`, `5min`, `15min`, `30min`, `60min`, `1hour`, `4hour`, `1day`, `1week`, `1month`, `1year`

### CalendarPartition

Timezone-aware partitions using format strings.

```python
from iosql.partition import CalendarPartition

# Daily partitions (YYYYMMDD)
partition = CalendarPartition(fmt='YYYYMMDD', tz='UTC', ts_col='date')

# Monthly partitions (YYYYMM)
partition = CalendarPartition(fmt='YYYYMM', tz='UTC', ts_col='date')
```

## API

### Database

```python
db = Database('sqlite://database/file.sqlite3')
t = db['table_name']
```

### Table Operations

```python
# Insert data
db['table'].insert_all(data, pk=['id', 'timestamp'], partition=partition)

# Upsert (insert or update based on pk)
db['table'].upsert_all(data, pk=['id', 'timestamp'])

# Query
db['table'].select('*', where={'field': 'timestamp', 'op': '>', 'value': ts})

# Delete
db['table'].delete({'field': 'timestamp', 'op': '>', 'value': ts})

# Get table info
db['table'].table_parts()           # List partition keys
db['table'].partition                # Get partition object
db['table'].partition = p             # Set partition (persisted)
```

### Where Clause

```python
where = {'field': 'timestamp', 'op': '>', 'value': 1705276800000}
where = {'field': 'timestamp', 'op': '<', 'value': 1705276800000}
where = {'field': 'timestamp', 'op': '>=', 'value': 1705276800000}
where = {'field': 'timestamp', 'op': '<=', 'value': 1705276800000}
where = {'field': 'timestamp', 'op': '==', 'value': 1705276800000}
where = {'field': 'timestamp', 'op': 'between_and', 'value': [t1, t2]}
```

## File Structure

```
database/
└── file.sqlite3/
    └── table_name/
        └── data/
            ├── meta.sqlite3        # Table metadata
            ├── 1705276800000.sqlite3  # Partition file (DurationPartition)
            └── 1705363200000.sqlite3
```

## Performance

- **Insert**: ~15,000 rows/second (batch insert)
- **Query**: Partition pruning enables O(1) partition access for time-range queries
- **200K rows** inserted in ~14 seconds across 3,334 partitions

## Notes

- Timestamps must be in **milliseconds**
- Partition boundaries are UTC-aligned
- `upsert_all` uses primary key for identity (no WHERE clause)
- `order_by` and `limit` are not supported for multi-partition queries

## ChangeLog

- v0.2.0 - New partition system with DurationPartition and CalendarPartition
  - Integer-based partition keys (milliseconds)
  - Auto-detection from metadata
  - Partition setter on table object
- v0.1.2 - Add create_index
- v0.1.1 - Add partition formats
- v0.1.0 - Initial release
