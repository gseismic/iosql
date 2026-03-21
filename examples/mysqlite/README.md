# MySQLite Quick Reference

## Quick Start

```python
from iosql.utils.mysqlite import Database

db = Database('mydata.sqlite3', auto_commit=True)

# No need to create table first - auto-create with ensure_table
db['users'].insert({'id': 1, 'name': 'Alice', 'age': 30}, ensure_table=True)
```

---

## Table of Contents

1. [Schemaless Mode](#schemaless-mode) - No table creation needed
2. [Basic CRUD](#basic-crud) - Create, Read, Update, Delete
3. [Batch Operations](#batch-operations) - Bulk insert
4. [Queries](#queries) - Select with filters
5. [Table Management](#table-management) - Create, drop, indexes
6. [Connection Pool](#connection-pool) - Thread-safe connections

---

## Schemaless Mode

### Direct Insert (Auto-Create Table)

```python
db['clicks'].insert({'user_id': 1, 'page': '/home'}, ensure_table=True)
db['clicks'].insert({'user_id': 2, 'page': '/products'})
# Table auto-created on first insert
```

### Dynamic Columns

```python
db['events'].insert({'id': 1, 'type': 'click'}, ensure_table=True)
db['events'].insert({'id': 2, 'type': 'scroll', 'delta_y': 300}, ensure_column=True)
db['events'].insert({'id': 3, 'type': 'keypress', 'key': 'Enter'}, ensure_column=True)
# Columns grow dynamically
```

### Batch Insert

```python
db['logs'].insert_all([
    {'level': 'INFO', 'msg': 'Started'},
    {'level': 'ERROR', 'msg': 'Failed', 'retry': 3},
], ensure_table=True)
```

### Replace & Ignore (Need Primary Key)

```python
db['cache'].insert({'key': 'user:1', 'value': 'Alice'}, pk='key', ensure_table=True)
db['cache'].insert({'key': 'user:1', 'value': 'Updated'}, replace=True)  # replaces

db['seen'].insert({'event_id': 100, 'ts': 123}, pk='event_id', ensure_table=True)
db['seen'].insert({'event_id': 100, 'ts': 999}, ignore=True)  # ignored
```

---

## Basic CRUD

### Create Table

```python
db['users'].create_table([
    {'name': 'id', 'type': 'INTEGER PRIMARY KEY'},
    {'name': 'name', 'type': 'TEXT', 'default': 'unknown'},
    {'name': 'age', 'type': 'INTEGER', 'default': 0},
])
```

### Insert

```python
db['users'].insert({'id': 1, 'name': 'Alice', 'age': 30})
db['users'].insert({'id': 2, 'name': 'Bob', 'age': 25}, replace=True)  # replace if exists
db['users'].insert({'id': 3, 'name': 'Charlie'}, ignore=True)  # skip if exists
```

### Select

```python
# All rows
db['users'].select_all('*')

# Single row
db['users'].select_one('*', where='id=1')

# Specific columns
db['users'].select_all(['name', 'age'])

# With filter
db['users'].select_all('*', where='age > 25')

# Ordered
db['users'].select_all('*', order_by='age DESC')

# Limited
db['users'].select_all('*', limit=10)
```

### Update

```python
db['users'].update({'age': 31}, where='id=1')
```

### Delete

```python
db['users'].delete(where='id=3')
db['users'].delete_all()  # delete all rows
```

### Count

```python
db['users'].count()              # total count
db['users'].count(where='age > 25')  # with filter
```

### Composite Primary Key

```python
# Multiple primary keys - useful for junction tables
db['order_items'].create_table([
    {'name': 'order_id', 'type': 'INTEGER'},
    {'name': 'product_id', 'type': 'INTEGER'},
    {'name': 'quantity', 'type': 'INTEGER'},
], pk=['order_id', 'product_id'])

db['order_items'].insert({'order_id': 1, 'product_id': 100, 'quantity': 2})
db['order_items'].insert({'order_id': 1, 'product_id': 101, 'quantity': 1})
db['order_items'].insert({'order_id': 2, 'product_id': 100, 'quantity': 5})

# Replace by composite key
db['order_items'].insert({'order_id': 1, 'product_id': 100, 'quantity': 99}, replace=True)

# Primary keys: ['order_id', 'product_id']
```

---

## Batch Operations

### Insert Many

```python
records = [
    {'id': i, 'name': f'user_{i}', 'score': i * 10}
    for i in range(100)
]
db['scores'].insert_all(records, ensure_table=True)
```

---

## Queries

### Raw SQL

```python
# Query returning dicts
db['users'].query_all("SELECT * FROM users WHERE age > 25")

# Aggregation
db['products'].query_all("SELECT category, COUNT(*) as cnt FROM products GROUP BY category")
```

---

## Table Management

### Check Existence

```python
db['users'].table_exists()  # True/False
```

### Add Column

```python
db['users'].ensure_column('phone', 'TEXT', default='unknown')
```

### Create Index

```python
db['users'].create_index('name')
db['users'].get_all_index()
db['users'].drop_index('users_name_index')
```

### List Tables

```python
db.tables()  # ['users', '_meta']
```

### Drop Table

```python
db['users'].drop_table()
```

### Table Info

```python
db['users'].column_info()     # column types, defaults
db['users'].default_values()   # default values
db['users'].pk()              # primary keys
```

---

## Connection Pool

```python
from iosql.utils.mysqlite.connection_pool import DatabaseConnectionPool

pool = DatabaseConnectionPool('mydata.sqlite3', max_connections=5)

with pool.get_connection() as conn:
    conn.execute('CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT)')
    conn.execute("INSERT INTO items VALUES (1, 'first')")
    result = conn.execute("SELECT * FROM items").fetchall()

pool.force_close_all_connections()
```

---

## API Summary

| Method | Description |
|--------|-------------|
| `insert(kv, ensure_table=False, ensure_column=False, pk=None, replace=False, ignore=False)` | Insert single row |
| `insert_all(data, ensure_table=False, ensure_column=False)` | Batch insert |
| `select_all(fields, where=None, order_by=None, limit=None)` | Select multiple rows |
| `select_one(fields, where=None, order_by=None)` | Select single row |
| `update(kv, where)` | Update rows |
| `delete(where)` | Delete rows |
| `delete_all()` | Delete all rows |
| `count(where=None)` | Count rows |
| `query_all(sql)` | Raw SQL returning dicts |
| `create_table(columns, pk=None)` | Create table |
| `drop_table()` | Drop table |
| `ensure_column(name, type, default=None)` | Add column if not exists |
| `create_index(column)` | Create index |
| `table_exists()` | Check if table exists |
| `column_info()` | Get column info |
| `default_values()` | Get default values |
