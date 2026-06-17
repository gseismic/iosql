# PLAN-023-mysqlite-standalone-hardening-OUTCOME

## 结论

本轮完成 `iosql.utils.mysqlite` 独立使用前的基础硬化，并回归检查了 iosql 主库 SQLite 分区表实现。重点修复了连接池文件句柄上限、关闭后复用、非主线程创建、事务语义、SQL 参数化、输入数据被原地修改，以及多分区扫描时分区 SQLite 文件缓存过大的风险。

## 已完成

1. 重写 `DatabaseConnectionPool`
   - 从线程永久缓存改为有上限的可归还连接池。
   - `max_connections` 现在约束单个 `Database` 实例创建的连接总数。
   - 等待连接的线程在连接池关闭后会退出，不会永久阻塞。
   - 增加 `get_pool_status()` / `is_pool_healthy()`。
2. 修复 `Database` 生命周期
   - 去掉默认 `SIGINT` / `SIGTERM` 注册，非主线程可创建 `Database`。
   - `close()` / `force_close_all_connections()` 后实例不可继续使用。
   - 增加 `get_connection_pool_status()` / `is_connection_pool_healthy()`。
   - 增加 `transaction()` 上下文，`commit()` / `rollback()` 只在事务上下文中可用。
3. 修复 SQL 构造
   - `insert()` 改为参数化写入。
   - 建表、索引、插入、更新、删除、计数等内部 SQL 统一 quote 表名/列名。
   - 保留 `where` / `query_all()` 原始 SQL 能力，但文档标注为高级逃生口。
   - 修复字符串默认值、保留字表名/列名、带双引号字符串写入。
4. 修复批量写入
   - `insert_all()` 不再原地修改调用方传入的 dict。
   - `ensure_table` / `ensure_column` 在同一个连接内完成，避免批量事务里跨连接建表。
   - 修复 integer/float 混合类型推断为 `float`。
5. 加固 `iosql.sqlite.Table`
   - 分区数据库缓存改为 LRU 式裁剪，不再一次性关闭所有分区连接。
   - 多分区扫描后打开的 part DB 缓存数量受 `_num_opened_max_allowed` 约束。
   - 参数化写入后，旧式无分区元数据中的 `None` 键值仍规范为字符串 `"None"`，保持兼容。
   - 去掉 JSON 元数据为手工拼 SQL 做的双引号转义，避免参数化后写坏 JSON。
6. 文档同步
   - 重写 `CONNECTION_POOL_CLEANUP.md`。
   - 更新 `iosql/utils/mysqlite/README.md` 的连接池和事务说明。
7. 额外修复
   - 修正 `tests/sqlite/_test_bkp.py` 中既有的字典语法错误，使全量 `compileall` 可以通过。

## Review 后继续修复的问题

1. 连接池满载等待时，如果另一个线程关闭连接池，等待线程可能阻塞；已改为带超时轮询关闭状态，并补测试。
2. 主库元数据 JSON 仍保留旧手工 SQL 转义；参数化后会写坏 JSON，已移除。
3. 旧式无分区表元数据把 `None` 写入复合主键；参数化后变成 SQL NULL，已显式规范为 `"None"`。
4. 连接池文档仍描述旧 signal handler 和不存在的包装 API；已同步到当前实现。

## 验证结果

已运行：

```bash
python -m pytest tests/utils/mysqlite -q
python -m pytest tests/sqlite/test_timeseries_contract.py -q
python -m pytest tests/sqlite -q
python -m pytest tests/test_partition_integration.py tests/test_partition.py -q
python examples/mysqlite/01_schemaless_crud.py
python examples/mysqlite/02_bulk_query_and_conflict.py
python examples/mysqlite/03_connection_pool_cleanup.py
python examples/05_partition_segments_smoke.py
python -m compileall -q iosql examples tests
git diff --check
```

结果：

- `tests/utils/mysqlite`：`41 passed`。
- `tests/sqlite/test_timeseries_contract.py`：`31 passed`。
- `tests/sqlite`：`33 passed, 11 skipped`。
- `tests/test_partition_integration.py tests/test_partition.py`：`72 passed`。
- 4 个示例脚本运行通过。
- 全量 compileall 通过。
- diff check 通过。
- pytest cache warning 来自父目录 `.pytest_cache` 只读，不影响测试结果。

## 保留说明

`iosql.utils.mysqlite` 仍位于 `iosql` 包内，本轮没有拆成独立发布包。当前已先稳定其作为独立模块使用所需的基础契约；后续若要抽包，应再清理 `connection_pool_bkp.py` 和 `backup/` 等历史资料，并设计独立包名、版本号和发布流程。
