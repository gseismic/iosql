# PLAN-023-mysqlite-standalone-hardening

## 背景

`iosql.utils.mysqlite` 目前既是 iosql SQLite 后端的内部工具层，也被整理为可直接使用的低层 SQLite API。代码审查发现它离“可作为独立模块稳定使用”还有几个基础契约问题：

1. 连接池按线程永久缓存连接，`max_connections` 不能约束总打开文件数。
2. `Database.close()` 后仍可继续写入。
3. `Database` 初始化时注册进程信号处理器，非主线程创建会失败。
4. `insert()` 等路径直接拼 SQL，字符串转义、保留字标识符和参数化不完整。
5. `auto_commit=False` / `commit()` 事务语义不成立。
6. `insert_all()` 会原地修改用户输入。
7. `iosql.sqlite.Table` 同时访问多个分区文件时需要确认不会长期打开过多 SQLite 文件。

## 目标

1. 修复 `mysqlite` 连接池，使连接数严格受 `max_connections` 约束。
2. 修复 `Database` 生命周期：关闭后不可继续使用，非主线程可创建实例。
3. 提供明确可用的事务上下文，避免伪 `commit()`。
4. 修复 SQL 构造：值参数化，表名/列名统一 quote。
5. 避免写入路径修改调用方输入数据。
6. 检查并加固 `iosql.sqlite.Table` 的分区数据库缓存和关闭策略。
7. 增加覆盖上述行为的回归测试。

## 非目标

1. 不把 `mysqlite` 从 `iosql` 包中拆出去；本轮先稳定内部契约。
2. 不移除 `where` / `query_all()` 原始 SQL 能力，只明确它们是高级逃生口。
3. 不重写 iosql 主库的分区/segment 逻辑。

## 实施计划

1. 重写 `DatabaseConnectionPool`：
   - 使用有上限的队列池。
   - checkout 时创建或等待连接，归还时放回池。
   - `force_close_all_connections()` 关闭空闲连接并让已 checkout 的连接归还时关闭。
2. 更新 `Database`：
   - 移除默认 signal 注册，只保留 atexit 清理。
   - `get_connection()` 检查关闭状态。
   - 新增 `transaction()` 上下文。
3. 更新 SQL helper 和 `Table`：
   - 建表、索引、插入、更新、查询的标识符 quote。
   - `insert()` 改用参数化执行。
   - `insert_all()` 不修改输入。
   - 修正类型合并。
4. 更新 `iosql.sqlite.Table.get_part_db()`：
   - 明确 LRU 关闭策略。
   - 确保达到分区文件缓存上限时关闭最旧分区连接。
5. 补充测试并运行：
   - `tests/utils/mysqlite`
   - `tests/sqlite/test_timeseries_contract.py`
   - `tests/test_partition_integration.py`
   - `python -m compileall -q iosql examples tests`
   - `git diff --check`
