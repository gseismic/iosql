# PLAN-022-current-first-example-audit

## 背景

`iosql_mac` 是同一仓库停在 `15efef6` 的旧 checkout，当前 `master` 已推进到 `f29e214`。旧目录没有独有提交，只是落后当前 11 个提交。

本轮目标不是回退或合并旧代码，而是以当前实现为准，筛选旧目录里仍能补齐当前学习入口和验证场景的内容。

## 旧目录评估

### 可以借鉴

1. `examples/mysqlite/` 覆盖了仍存在的低层 `iosql.utils.mysqlite` API：
   - schema-on-write
   - CRUD
   - `insert_all`
   - 原始 SQL 查询
   - replace/ignore
   - 连接池清理
2. `examples/04_large_partition_demo.py` 的价值在于展示分区规模写入和查询窗口，而不是旧写法本身。
3. `examples/05_connection_pool_cleanup_demo.py` 的价值在于提供可运行的资源释放示例。

### 不应借鉴

1. 旧分区类实现。当前 `CalendarPartition` 已修正时区解析，`DurationPartition` 已扩展别名支持。
2. 旧 `pyproject.toml`。当前打包入口、动态版本和 Python 版本要求更清晰。
3. 旧目录未跟踪的 `.agents/skills/iosql`。它可以作为结构参考，但内容引用旧示例和旧 API，不能原样复制。
4. 旧目录中的 `__pycache__` 等运行产物。

## 目标

1. 保持当前 `Database.timeseries()`、`write()`、`query()`、`layout_report()`、`segments()` 为主线。
2. 新增一个当前 API 的分区/segment 规模示例，继承旧大分区 demo 的验证意图。
3. 恢复少量 mysqlite 示例入口，但只覆盖确定有用、仍被测试支持的低层功能。
4. 更新示例索引和 mysqlite README，避免用户误以为低层 mysqlite 是新的主 API。

## 非目标

1. 不恢复旧的 `examples/01_duration_ingest.py`、`02_calendar_schedule.py`、`03_composite_quotes.py`。
2. 不恢复全部旧 mysqlite 示例，只保留低层 API 最常用路径。
3. 不改变核心分区、segment 或查询逻辑。
4. 不引入旧 API 到 README 的主路径。

## 实施计划

1. 新增 `examples/05_partition_segments_smoke.py`：
   - 使用 `Database.timeseries()`。
   - 小规模生成多天、多 symbol 数据。
   - 使用较低 `max_rows_per_file` 触发物理 segment。
   - 输出逻辑分区、物理 segment、布局报告和时间窗口查询计划。
2. 新增 `examples/mysqlite/`：
   - `01_schemaless_crud.py`
   - `02_bulk_query_and_conflict.py`
   - `03_connection_pool_cleanup.py`
3. 重写 `iosql/utils/mysqlite/README.md` 为当前可用的低层 API 快速参考。
4. 更新 `examples/README.md` 和根 `README.md` 的示例索引。

## 验证

1. 运行新增示例：
   - `python examples/05_partition_segments_smoke.py`
   - `python examples/mysqlite/01_schemaless_crud.py`
   - `python examples/mysqlite/02_bulk_query_and_conflict.py`
   - `python examples/mysqlite/03_connection_pool_cleanup.py`
2. 运行相关测试：
   - `python -m pytest tests/sqlite/test_timeseries_contract.py tests/utils/mysqlite -q`
3. 运行静态检查：
   - `python -m compileall -q iosql examples tests/sqlite/test_timeseries_contract.py tests/utils/mysqlite`
   - `git diff --check`
