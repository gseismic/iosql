# PLAN-043 SQLite 到 PostgreSQL 易用迁移接口实施结果

## 结论

已提供从 SQLite 版 iosql 迁移到 PostgreSQL 版 iosql 的易用接口。

Python 整库迁移：

```python
source = Database("sqlite://db/market.iosql")
result = source.to_postgresql(
    "postgresql://user:pass@localhost:5432/market",
    tables=["daily_bars", "kline_1m"],
    batch_size=50_000,
)
```

Python 单表迁移：

```python
ticks = source.table("kline_1m")
result = ticks.migrate_to(
    "postgresql://user:pass@localhost:5432/market",
    target_table="pg_kline_1m",
    batch_size=50_000,
)
```

CLI：

```bash
iosql migrate \
  -s sqlite://db/market.iosql \
  --target postgresql://user:pass@localhost:5432/market \
  --table kline_1m:pg_kline_1m \
  --batch-size 50000
```

## 已完成

### 迁移结果类型

新增：

- `TableMigrationResult`
- `MigrationResult`

结果包含源 URI、目标 URI、表名、目标表名、迁移行数和批次数，支持 `to_dict()` 输出，便于 CLI JSON 返回和脚本审计。

### SQLite 源表批量读取

SQLite `Table` 新增：

- `iter_rows(batch_size=10_000)`
- `migrate_to(target_uri, target_table=None, mode='append', batch_size=10_000)`

`iter_rows()` 按当前可见物理 refs 逐个 SQLite 文件读取，并使用 `LIMIT/OFFSET` 控制每批最大行数。它不会一次性把全库或整表读入内存，适合海量 K 线迁移。

### Database 级迁移

高层 `Database` 新增：

- `migrate_to(target_uri, tables=None, mode='append', batch_size=10_000)`
- `to_postgresql(target_uri, tables=None, mode='append', batch_size=10_000)`

`tables` 支持：

- `None`：迁移全库；
- `["ticks", "daily"]`：迁移指定表；
- `{"ticks": "pg_ticks"}`：迁移并重命名目标表。

### PostgreSQL 主 API 支持

PostgreSQL 引擎补齐：

- `use_contract()` / `use_spec()`
- `spec()`
- `write()`
- `do_insert_all()`
- `do_create_table()`

并修复/增强：

- 支持 `Database("postgresql://...").table(name, spec=...)`。
- 建表时使用表级复合主键，不再对多个 pk 列分别声明 `PRIMARY KEY`。
- SQLite 常用类型映射到 PostgreSQL 类型：
  - `integer/int/bigint` -> `BIGINT`
  - `float/real/double` -> `DOUBLE PRECISION`
  - `text/string/str` -> `TEXT`
  - `bool` -> `BOOLEAN`
  - `json` -> `JSONB`
- native partition 子表名做安全化处理。
- `insert_all()` / `write()` 返回 `WriteResult`。

### CLI

新增：

- `iosql migrate`

行为：

- source 仍要求 SQLite iosql 目录库；
- target 支持 `postgresql://`、`sqlite://`、本地路径；
- `--table source:target` 支持表重命名；
- 默认 `mode=append`；
- 输出 `MigrationResult` JSON。

### 安装与文档

- `pyproject.toml` 增加可选依赖：

```toml
[project.optional-dependencies]
postgresql = ["psycopg2-binary>=2.9"]
```

已更新：

- `README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/api-reference.md`
- `skills/iosql-usage/references/usage-patterns.md`
- `skills/iosql-usage/references/troubleshooting.md`

## 测试

已运行：

```bash
python -m compileall iosql tests -q
python -m pytest tests/test_migration.py tests/test_cli.py -q
```

结果：

```text
23 passed in 7.29s
```

已运行：

```bash
python -m compileall iosql tests -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/engines/postgresql/test_postgresql.py tests/engines/sqlite/test_timeseries_contract.py tests/engines/sqlite/test_partitioned_table_contract.py -q
```

结果：

```text
85 passed, 1 skipped in 23.21s
```

已运行：

```bash
python -m compileall iosql tests -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/engines/postgresql tests/core/query tests/utils/mysqlite -q
```

结果：

```text
231 passed, 13 skipped in 35.35s
```

已运行：

```bash
python -m pytest tests/test_partition_integration.py -q -k 'not test_upsert_large_data'
```

结果：

```text
41 passed, 1 deselected in 17.28s
```

说明：真实 PostgreSQL 集成测试仍由 `POSTGRES_DSN` 控制；本机未提供 PostgreSQL 服务时按既有测试策略 skip。新增迁移测试使用 fake target DB 验证不依赖外部服务的批量迁移语义。

## 保留边界

1. PostgreSQL 引擎当前优先覆盖契约建表、批量写入和迁移目标；复杂 `latest/asof/explain_select` 优化仍以 SQLite 引擎更完整。
2. SQLite row segment 不映射成 PostgreSQL row segment；PostgreSQL 侧使用表契约和 native partition。
3. 本轮不实现增量同步、断点续传或源/目标哈希校验。
4. PostgreSQL 分区表的复合主键需要满足 PostgreSQL native partition 约束，例如唯一键应包含 partition key。

## 下一步建议

下一步高价值目标仍建议是 **PLAN-044：迁移校验与断点续传**。

理由：

1. 现在已经有易用迁移入口，但生产级海量 K 线迁移需要结果校验。
2. 大规模迁移可能中断，需要按表/分区/segment 记录进度并恢复。
3. PostgreSQL 目标查询能力尚未完全对齐 SQLite；迁移后的校验命令能先降低落库风险。

建议 PLAN-044 覆盖：

- `iosql migrate --dry-run` 显示表、行数、目标表、批次数估算；
- `iosql migrate --verify row-count` 校验源/目标行数；
- 可选按 table/partition 记录 checkpoint；
- 失败后可 `--resume` 从上次成功批次继续；
- 输出迁移报告 JSON/Markdown。
