# PLAN-050 迁移体验收口与发布前审计 OUTCOME

## 结果

已完成 SQLite / PostgreSQL 双向迁移体验收口。

本轮根据用户补充要求，新增了 PostgreSQL 作为迁移源写回 SQLite 的能力，并新增 `sqlite -> postgresql -> sqlite` 双向转换示例。

## 实现内容

### PostgreSQL -> SQLite 迁移

PostgreSQL `Table` 新增作为迁移源的能力：

- `iter_rows(batch_size=...)`
- `migrate_to(target_uri, target_table=None, mode="append", batch_size=..., verify=None, dry_run=False)`
- `_migrate_to_target_table(...)`
- `_migration_dry_run_result(...)`

行为与 SQLite 源表迁移保持一致：

- 按 `batch_size` 从 PostgreSQL 目标侧批量读取；
- 写入目标库的逻辑表；
- 支持 `verify="row-count"`；
- 支持 `dry_run=True` 估算源行数和批次数。

PostgreSQL 作为迁移源时，重新打开逻辑表需要绑定同一份 `TableContract`：

```python
pg = Database("postgresql://user:pass@localhost:5432/market")
pg.table("pg_ticks", spec=spec)
pg.migrate_to(
    "sqlite://db/from-postgresql.iosql",
    tables={"pg_ticks": "ticks"},
    verify="row-count",
)
```

### 双向转换示例

新增：

- `examples/12_sqlite_postgresql_roundtrip.py`

示例流程：

1. 构造 SQLite 源库；
2. 执行 `sqlite -> postgresql` dry-run；
3. 设置 `POSTGRES_DSN` 后执行真实 SQLite -> PostgreSQL 迁移和 row-count 校验；
4. 给 PostgreSQL 逻辑表绑定同一份表契约；
5. 执行 `postgresql -> sqlite` 迁移和 row-count 校验；
6. 查询回写后的 SQLite 目标表。

无 `POSTGRES_DSN` 时，示例只执行 dry-run，不连接 PostgreSQL。

### 测试

新增/扩展：

- mock PostgreSQL 分页 cursor，验证 PostgreSQL -> SQLite 批量迁移；
- `verify="row-count"` 验证源/目标行数；
- 真实 PostgreSQL 条件测试扩展为往返迁移，仍由 `POSTGRES_DSN` 控制；
- 示例 smoke 覆盖 `examples/11...` 和 `examples/12...` 的无 PostgreSQL dry-run 路径。

### 文档

已更新：

- `README.md`
- `examples/README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/api-reference.md`
- `skills/iosql-usage/references/usage-patterns.md`

新增说明：

- `examples/12_sqlite_postgresql_roundtrip.py`；
- PostgreSQL 可作为迁移源写回 SQLite；
- PostgreSQL 源表重新打开时需要绑定 `TableContract`；
- `Database.table_names()` 对 PostgreSQL 只返回逻辑表；
- `calendar(...)` 迁移支持 `text` / `date` / `timestamp` 目标列。

## 验证

已执行：

```bash
python examples/11_sqlite_to_postgresql_migration.py
python examples/12_sqlite_postgresql_roundtrip.py
python -m pytest tests/test_migration.py tests/engines/postgresql/test_postgresql.py -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/engines/postgresql tests/core/query tests/utils/mysqlite -q
python -m pytest tests/test_partition_integration.py -q -k 'not test_upsert_large_data'
python -m compileall iosql tests examples -q
git diff --check
```

结果：

- 示例 11 dry-run 通过；
- 示例 12 dry-run 通过；
- `12 passed, 3 skipped in 1.38s`；
- `249 passed, 15 skipped in 17.32s`；
- `41 passed, 1 deselected in 11.24s`；
- `compileall` 通过；
- `git diff --check` 通过。

## Review 结论

已 review 本轮收口：

1. SQLite -> PostgreSQL 和 PostgreSQL -> SQLite 都走 `Database.migrate_to()` / `Table.migrate_to()` 语义。
2. PostgreSQL 源迁移按批读取，不一次性加载整表。
3. row-count 校验在双向迁移中都可用。
4. 文档不再暗示 PostgreSQL 查询能力完全等同 SQLite；仍明确 `latest/asof/explain_select` 未优化。
5. 示例明确无 `POSTGRES_DSN` 不连接 PostgreSQL，设置后才执行真实迁移。

## 剩余边界

1. PostgreSQL 作为源库重新打开时，仍需要调用方提供 `TableContract`。
2. 未实现 checkpoint/resume。
3. PostgreSQL `latest/asof/explain_select` 仍未对齐 SQLite 引擎。
4. 当前环境没有可用 PostgreSQL 服务，真实 PostgreSQL 集成测试按 skip 机制跳过；有 `POSTGRES_DSN` 时会自动运行。

## 下一步建议

如继续推进，建议做 **PLAN-051：PostgreSQL 源表契约持久化与自动发现**。

范围建议：

- 在 PostgreSQL 侧保存 iosql 表契约元数据；
- 重新打开 PostgreSQL 表时自动恢复 `TableContract`；
- 进一步降低 PostgreSQL -> SQLite 迁移时的用户心智负担。
