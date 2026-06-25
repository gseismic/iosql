# PLAN-047 PostgreSQL 迁移验证与目标侧基础查询 OUTCOME

## 结果

已完成 PostgreSQL 迁移验证与目标侧基础查询增强。

本轮围绕“方便的 sqlite3 到 postgresql 转换，用户无需感知分区”补齐了迁移前预览、迁移后 row-count 校验、目标侧基础查询和 PostgreSQL 表名隐藏 native partition 子表。

## 实现内容

### 迁移 dry-run

Python API 新增：

- `Database.migrate_to(..., dry_run=True)`
- `Database.to_postgresql(..., dry_run=True)`
- `Table.migrate_to(..., dry_run=True)`

dry-run 行为：

- 不连接目标库；
- 读取源表契约；
- 通过 SQLite 物理 segment 执行 `count(*)` 统计源行数；
- 按 `batch_size` 估算批次数；
- 返回 `MigrationResult` / `TableMigrationResult`。

CLI：

```bash
iosql migrate \
  -s sqlite://db/market.iosql \
  --target postgresql://user:pass@localhost:5432/market \
  --dry-run \
  --batch-size 50000
```

### row-count 校验

Python API 新增：

- `verify="row-count"`

CLI 新增：

```bash
iosql migrate \
  -s sqlite://db/market.iosql \
  --target postgresql://user:pass@localhost:5432/market \
  --verify row-count
```

迁移结果现在记录：

- `source_rows`
- `target_rows`
- `verify`
- `verified`
- `dry_run`

若 row-count 不一致，会抛出 `BadArgument("row-count verification failed: ...")`，CLI 会失败退出。

### SQLite 源表统计

SQLite `Table` 新增：

- `count_rows()`

实现按当前可见物理 segment 分别执行 `count(*)`，不读取所有行到内存。

### PostgreSQL 目标侧查询

PostgreSQL `Table` 新增/增强：

- `count_rows(where=None)`
- `select(fields, where=None, order_by=None, limit=None, to_dict=True)`
- `iter_select(...)`
- `iter_select_batches(...)`

`where` 支持既有 dict/list/text where 表达，SQL 使用参数化占位符，不再拼接 where 值。`order_by` 只接受字段名 + asc/desc。

### PostgreSQL 逻辑表名

`Database("postgresql://...").table_names()` 改为从 `pg_class` 查询普通表和 partition parent，并过滤 `pg_inherits` 子表，避免把 native partition 子表暴露给用户。

## 测试

新增/更新测试覆盖：

- `Database.migrate_to(..., dry_run=True)` 不创建目标库连接；
- dry-run 返回源行数、目标表名和估算批次数；
- `verify="row-count"` 成功路径记录 `source_rows` / `target_rows` / `verified`；
- row-count mismatch 会抛出 `BadArgument`；
- CLI `iosql migrate --dry-run`；
- CLI `iosql migrate --verify row-count`；
- PostgreSQL `select()` / `iter_select_batches()` / `count_rows()` 使用参数化 where、limit、offset。

已执行：

```bash
python -m compileall iosql tests -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/engines/postgresql/test_postgresql.py -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/engines/postgresql tests/core/query tests/utils/mysqlite -q
git diff --check
```

结果：

- `30 passed, 1 skipped in 7.98s`
- `243 passed, 13 skipped in 27.41s`
- `git diff --check` 通过

## Review 结论

已 review 本轮核心变更：

1. dry-run 路径在 `Database.migrate_to()` 中提前返回，不调用 `_new_migration_target()`。
2. row-count 校验路径在写入完成后调用目标 `count_rows()`，失败时明确抛错。
3. PostgreSQL where 条件复用现有 `Where` / `Case` 解析，不手写值拼接。
4. 迁移结果新增字段保持向后兼容，原有 `rows_copied` / `batches` 属性不变。
5. PostgreSQL `table_names()` 过滤 partition 子表，符合“用户无需感知分区”的目标。

## 边界

1. `verify="row-count"` 适合全量迁移到空目标表；目标表已有旧数据或使用 `ignore/upsert` 时可能失败。
2. PostgreSQL 仍未实现 `latest/asof/explain_select` 优化。
3. 本轮不实现断点续传、checkpoint 或哈希校验。
4. PostgreSQL native partition 仍沿用既有迁移策略，不把 SQLite row segment 映射为 PostgreSQL row segment。

## 下一步建议

建议下一步做 **PLAN-048：PostgreSQL 迁移真实集成验证与文档化示例**。

范围建议：

- 在 `POSTGRES_DSN` 环境下增加真实迁移 + row-count verify 集成测试；
- 增加一个 SQLite 到 PostgreSQL 的完整示例脚本；
- 明确 calendar/date 分区迁移到 PostgreSQL native partition 的支持边界；
- 在 README 中给出生产迁移 checklist。
