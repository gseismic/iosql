# 连接池清理和监控

## 概述

`iosql.utils.mysqlite` 使用有上限的 SQLite 连接池，避免长时间运行或多分区扫描时无限打开 SQLite 文件。连接通过 `with db.get_connection()` 借出，退出上下文后归还连接池。

连接池不再按线程永久缓存连接；`max_connections` 表示同一 `Database` 实例最多创建的 SQLite 连接总数。

## 基本使用

```python
from iosql.utils.mysqlite import Database

db = Database("example.db", max_connections=4)

with db.get_connection() as conn:
    conn.execute("SELECT 1")

db.close()
```

`Database.close()` 会关闭连接池并标记实例为已关闭。关闭后继续使用该实例会抛出异常；如需重新访问同一文件，请创建新的 `Database` 实例。

## 强制关闭

```python
db.force_close_all_connections()
```

`force_close_all_connections()` 用于异常恢复或资源紧急释放。调用后连接池不可复用，行为等同于关闭该 `Database` 实例。

## 连接池状态

```python
status = db.get_connection_pool_status()
print(status)
print(db.is_connection_pool_healthy())
```

状态字段：

| 字段 | 说明 |
| --- | --- |
| `max_connections` | 连接池最大连接数。 |
| `created_connections` | 当前已创建且未彻底关闭的连接数。 |
| `available_connections` | 空闲可复用连接数。 |
| `checked_out_connections` | 当前借出的连接数。 |
| `closed` | 连接池是否已关闭。 |
| `db_file` | SQLite 文件路径。 |

`is_connection_pool_healthy()` 目前检查 `created_connections <= max_connections`。

## 自动清理

模块会注册 `atexit` 清理函数，在 Python 进程正常退出时尝试关闭仍活跃的 `Database` 实例。

模块不会注册 `SIGINT` / `SIGTERM` 处理器。信号处理属于应用层职责，这样可以保证 `Database` 能在非主线程中创建，也避免库代码接管宿主进程的退出行为。

## 事务

多步写入需要原子性时使用 `transaction()`：

```python
with db.transaction():
    db["items"].insert({"id": 1}, pk="id", ensure_table=True)
    db["items"].insert({"id": 2})
```

上下文正常退出时提交，发生异常时回滚。`commit()` / `rollback()` 只在活跃 `transaction()` 上下文中可用。

## 最佳实践

1. 使用 `with Database(...) as db:` 或显式调用 `db.close()`。
2. 为长时间运行程序设置合理 `max_connections`。
3. 不要在 `close()` 或 `force_close_all_connections()` 后继续复用同一个 `Database` 实例。
4. 对用户输入使用参数化写入路径；`where` 和 `query_all()` 是原始 SQL 逃生口，不应拼接未验证外部输入。
