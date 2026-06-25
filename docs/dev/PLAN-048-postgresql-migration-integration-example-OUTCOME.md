# PLAN-048 PostgreSQL 迁移真实集成验证与文档化示例 OUTCOME

## 结果

已完成 PostgreSQL 迁移真实集成测试入口、示例脚本和生产迁移文档补充。

本轮重点是把 `PLAN-047` 的迁移能力变成用户可运行、可验证的流程：先 dry-run，再迁移，再 row-count 校验，最后在 PostgreSQL 目标侧按逻辑表查询。

## 实现内容

### PostgreSQL 集成测试

`tests/engines/postgresql/test_postgresql.py` 新增真实集成测试：

- 构造临时 SQLite 源库；
- 写入 duration 分区 + row segment 表；
- 执行 `to_postgresql(..., dry_run=True)`；
- 执行 `to_postgresql(..., verify="row-count")`；
- 在 PostgreSQL 目标侧执行 `table_names()`、`count_rows()` 和 `select()`；
- 验证 `table_names()` 不返回 native partition 子表。

该测试继续沿用既有 `POSTGRES_DSN` 机制；当前环境未提供 PostgreSQL 服务时按预期 skip。

### PostgreSQL 逻辑表名 mock 测试

`tests/test_migration.py` 新增 mock 测试：

- 验证 `Database("postgresql://...").table_names()` 查询包含 `pg_inherits` 过滤；
- 确认返回的是逻辑表名，不是 partition child。

### 示例脚本

新增 `examples/11_sqlite_to_postgresql_migration.py`：

- 默认准备 `examples/runtime/11_sqlite_source.iosql`；
- 未设置 `POSTGRES_DSN` 时只执行 dry-run，不连接 PostgreSQL；
- 设置 `POSTGRES_DSN` 后重建示例目标表 `iosql_example_migration_ticks`；
- 执行真实迁移、`verify="row-count"` 和目标侧查询。

运行方式：

```bash
python examples/11_sqlite_to_postgresql_migration.py
POSTGRES_DSN=postgresql://user:pass@localhost:5432/market \
  python examples/11_sqlite_to_postgresql_migration.py
```

### 文档

已更新：

- `README.md`
- `examples/README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/usage-patterns.md`

新增说明：

- 第 11 个 PostgreSQL 迁移示例；
- 生产迁移 checklist；
- `POSTGRES_DSN` 行为；
- `Database.table_names()` 返回逻辑表，不暴露 native partition 子表；
- `duration(...)` / `range(...)` 更适合直接映射 PostgreSQL native range；
- `calendar(...)` / 日期字符串分区迁移前应在测试库验证目标列值和 range 边界一致性。

## 验证

已执行：

```bash
python examples/11_sqlite_to_postgresql_migration.py
python -m pytest tests/test_migration.py tests/engines/postgresql/test_postgresql.py -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/engines/postgresql tests/core/query tests/utils/mysqlite -q
python -m pytest tests/test_partition_integration.py -q -k 'not test_upsert_large_data'
python -m compileall iosql tests examples -q
git diff --check
```

结果：

- 示例 dry-run 通过，输出 `source_rows=4`、`batches=2`；
- `7 passed, 2 skipped in 0.46s`；
- `244 passed, 14 skipped in 22.92s`；
- `41 passed, 1 deselected in 15.17s`；
- `compileall` 通过；
- `git diff --check` 通过。

## Review 结论

已 review 本轮变更：

1. 真实 PostgreSQL 测试仍受 `POSTGRES_DSN` 控制，不会要求本地必须有服务。
2. 示例未设置 `POSTGRES_DSN` 时不会尝试连接 PostgreSQL。
3. 示例只重建默认示例目标表，避免误操作普通业务表。
4. 文档明确用户只需要看逻辑表，native partition 子表由 PostgreSQL 管理。
5. calendar/date 分区的 PostgreSQL native range 边界仍作为已知边界明确提示。

## 边界

1. 本机当前没有可用 PostgreSQL 服务，因此真实迁移集成测试处于 skip；已有测试会在 `POSTGRES_DSN` 可用时自动运行。
2. 本轮仍不实现 checkpoint/resume。
3. 本轮不改变 calendar partition 到 PostgreSQL native range 的底层策略。

## 下一步建议

建议下一步做 **PLAN-049：PostgreSQL calendar/date 分区策略硬化**。

范围建议：

- 明确 PostgreSQL native partition 对 `CalendarPartition` 的列类型要求；
- 对 text/date/timestamp 目标列生成正确 range bound；
- 增加 calendar/date 迁移真实或 mock 测试；
- 文档中区分 duration/range/calendar 三种迁移策略。
