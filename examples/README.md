# Examples

Numbered scripts in this directory are the main entry points for running iosql demos.

## Partition Demos

- `01_duration_ingest.py` - inserts 2 days of minute-level data into daily partitions.
- `02_calendar_schedule.py` - stores calendar-based events with `CalendarPartition`.
- `03_composite_quotes.py` - shows composite primary keys with partitioned quote data.
- `04_large_partition_demo.py` - stress test with 500K rows across 50 partitions.
- `05_connection_pool_cleanup_demo.py` - demonstrates connection pooling and forced cleanup.

## Runtime Data

- Generated files live under `examples/runtime/` and are ignored by git.
- Partition demos write to `examples/runtime/partition/`.
- mysqlite demos write to `examples/runtime/mysqlite/`.

## Running

```bash
python examples/01_duration_ingest.py
python examples/04_large_partition_demo.py
python examples/05_connection_pool_cleanup_demo.py
```

## mysqlite Demos

Legacy sqlite utility examples remain in `examples/mysqlite/` and follow the same numbered naming pattern.
