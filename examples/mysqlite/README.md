# MySQLite Examples

This folder contains numbered demos for `iosql.utils.mysqlite`.

## File Index

| File | Description |
|------|-------------|
| `00_schemaless.py` | Auto-create tables and grow columns on insert |
| `01_basic_crud.py` | Basic create, read, update, delete flow |
| `02_batch_insert.py` | Batch insert and simple aggregation |
| `03_queries.py` | Query helpers and filters |
| `04_table_management.py` | Table, column, and index management |
| `05_connection_pool.py` | Thread-bound connection pool demo |
| `06_composite_pk.py` | Composite primary key usage |
| `07_demo_basic.py` | Basic defaults and inserts |
| `08_demo_advanced.py` | Replace/ignore and schema evolution |

## Runtime Data

Generated SQLite files now live under `examples/runtime/mysqlite/` and are ignored by git.
Each script creates its target directory before opening the database.

## Notes

- Keep examples numbered to match the repository convention.
- Use `Database(..., auto_commit=True)` for the small demo scripts unless the example is specifically about transaction handling.
- Prefer short, self-contained outputs so the script can be run directly from the command line.
