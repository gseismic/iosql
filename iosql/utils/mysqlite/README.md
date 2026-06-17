# iosql.utils.mysqlite

`iosql.utils.mysqlite` 是 iosql 仍在使用的低层 SQLite 工具层。它直接面向单个 SQLite 文件，提供基于字典的建表、写入、查询和连接池能力。

新代码应优先使用主 API：`Database.table(..., pk=..., partition=..., segment=...)`。只有在需要直接操作单文件 SQLite、快速 schema-on-write 或复用低层连接池时，才使用本模块。

## 基本入口

```python
from iosql.utils.mysqlite import Database

db = Database("examples/runtime/mysqlite/demo.sqlite3", auto_commit=True)
table = db["events"]
```

`Database` 会按需创建目录和 SQLite 文件。使用完成后调用 `db.close()`；关闭后的实例不可继续使用，需要重新创建 `Database`。

## 自动建表和动态加列

```python
table.insert(
    {"id": 1, "kind": "click", "page": "/home"},
    pk="id",
    ensure_table=True,
)
table.insert(
    {"id": 2, "kind": "scroll", "delta_y": 320},
    ensure_column=True,
)
```

- `ensure_table=True`：目标表不存在时按输入字段自动建表。
- `ensure_column=True`：输入出现新字段时自动增加列。
- `pk=`：建表时声明主键，可传字符串或字符串列表。

## 批量写入

```python
table.insert_all(
    [
        {"order_id": 1, "line_id": 1, "sku": "A", "qty": 2},
        {"order_id": 1, "line_id": 2, "sku": "B", "qty": 1},
    ],
    ensure_table=True,
    pk=["order_id", "line_id"],
    batch_size=5000,
)
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `ensure_table` | 表不存在时自动建表。 |
| `ensure_column` | 字段缺失时自动加列。 |
| `replace` | 主键冲突时覆盖。 |
| `ignore` | 主键冲突时跳过。 |
| `batch_size` | 批量写入分批大小。 |
| `commit` | 是否在批次结束后提交。 |

## 查询和修改

```python
rows = table.select_all("*", where="qty > 1", order_by="order_id,line_id")
row = table.select_one("*", where="order_id=1 AND line_id=1")
summary = table.query_all(
    "SELECT sku, SUM(qty) AS total_qty FROM orders GROUP BY sku"
)

table.update({"qty": 3}, where="order_id=1 AND line_id=1")
table.delete(where="order_id=1 AND line_id=2")
```

`mysqlite` 的 `where` 是原始 SQL 字符串。不要把未验证的外部输入直接拼入 `where` 或 `query_all()`。

## 连接池

```python
db = Database("demo.sqlite3", max_connections=4)

with db.get_connection() as conn:
    conn.execute("SELECT 1")

db.force_close_all_connections()
```

连接池用于减少重复打开 SQLite 文件的成本，并避免长时间运行时泄露文件句柄。`max_connections` 是单个 `Database` 实例的连接总数上限。

可通过 `db.get_connection_pool_status()` 和 `db.is_connection_pool_healthy()` 查看连接池状态。`force_close_all_connections()` 会关闭连接池并使当前 `Database` 实例不可复用。更多说明见 `CONNECTION_POOL_CLEANUP.md`。

## 事务

```python
with db.transaction():
    table.insert({"id": 1, "name": "Alice"}, pk="id", ensure_table=True)
    table.insert({"id": 2, "name": "Bob"})
```

`transaction()` 正常退出时提交，异常退出时回滚。`commit()` 和 `rollback()` 只在活跃事务上下文中可用。

## 示例

可运行示例位于 `examples/mysqlite/`：

| 文件 | 场景 |
| --- | --- |
| `01_schemaless_crud.py` | 自动建表、动态加列、基础 CRUD。 |
| `02_bulk_query_and_conflict.py` | 批量写入、聚合查询、复合主键、replace/ignore。 |
| `03_connection_pool_cleanup.py` | 线程绑定连接、强制释放和重新打开数据库。 |
