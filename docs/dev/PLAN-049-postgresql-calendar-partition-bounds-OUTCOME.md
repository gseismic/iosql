# PLAN-049 PostgreSQL calendar/date 分区策略硬化 OUTCOME

## 结果

已完成 PostgreSQL native partition 下 calendar/date 分区边界生成的硬化。

本轮解决的问题是：SQLite calendar 分区迁移到 PostgreSQL 时，不再把 `CalendarPartition.get_range()` 的 UTC 毫秒值直接作为 PostgreSQL range bound，而是根据目标列类型生成可比较的边界。

## 实现内容

### 类型感知 partition bound

`iosql/engines/postgresql/table.py` 新增内部逻辑：

- `_partition_bounds(partition, part_key, rows=None)`
- `_calendar_partition_bounds(partition, part_key, rows=None)`
- `_partition_column_type(partition)`
- `_calendar_text_bounds(...)`

行为：

- `duration(...)` / `range(...)`：继续使用数值边界，行为不变。
- `calendar(...)` + `text`：
  - 根据写入行样本判断使用 ISO 文本边界或 compact 文本边界；
  - 例如 `2026-01-05` -> `("2026-01-05", "2026-01-06")`；
  - 例如 `20260105` -> `("20260105", "20260106")`。
- `calendar(...)` + `date`：
  - 输出 `YYYY-MM-DD` 边界。
- `calendar(...)` + `timestamp` / `datetime`：
  - 按 partition timezone 输出 `YYYY-MM-DD HH:mm:ss` 边界。

PostgreSQL `Table` 现在会记录声明列类型，用于 native partition bound 生成。

### 测试

新增 mock 测试：

- `test_postgresql_calendar_partition_uses_iso_text_bounds`
- `test_postgresql_calendar_partition_uses_compact_text_bounds`
- `test_postgresql_calendar_partition_uses_date_column_bounds`
- `test_postgresql_duration_partition_keeps_numeric_bounds`

新增可选真实 PostgreSQL 测试：

- `test_sqlite_calendar_text_migration_verify_and_query`

真实测试仍由 `POSTGRES_DSN` 控制；当前环境未提供 PostgreSQL 服务时按预期 skip。

### 文档

已更新：

- `README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/usage-patterns.md`

文档现在说明：

- `calendar(...)` 迁移支持 `text` / `date` / `timestamp` 目标列；
- text 日期列建议统一使用 ISO 日期或 compact 日期，不要在同一列混用格式；
- 用户不需要直接操作 PostgreSQL native partition 子表。

## 验证

已执行：

```bash
python -m pytest tests/test_migration.py tests/engines/postgresql/test_postgresql.py -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/engines/postgresql tests/core/query tests/utils/mysqlite -q
python -m pytest tests/test_partition_integration.py -q -k 'not test_upsert_large_data'
python examples/11_sqlite_to_postgresql_migration.py
python -m compileall iosql tests examples -q
git diff --check
```

结果：

- `11 passed, 3 skipped in 0.71s`
- `248 passed, 15 skipped in 28.84s`
- `41 passed, 1 deselected in 19.22s`
- 示例 dry-run 通过；
- `compileall` 通过；
- `git diff --check` 通过。

## Review 结论

已 review 本轮核心变更：

1. duration/range 数值分区边界保持原行为。
2. calendar/text 边界由首行样本决定 ISO 或 compact 格式，避免把 UTC 毫秒写入 text range。
3. calendar/date 和 calendar/timestamp 使用目标列可接受的 ISO 字符串。
4. 文档不再要求用户手动理解 native partition 边界，只保留“同一 text 日期列不要混用格式”的数据质量约束。

## 边界

1. text 日期列如果混用 `2026-01-05` 和 `20260105` 两种格式，PostgreSQL range 比较仍无法同时保证正确。
2. 本轮不实现 checkpoint/resume。
3. PostgreSQL `latest/asof/explain_select` 仍未对齐 SQLite 引擎。

## 下一步建议

建议下一步做 **PLAN-050：迁移体验收口与发布前审计**。

范围建议：

- 汇总 043-049 的迁移链路做端到端文档审计；
- 检查 README / skill / API reference 是否存在过时描述；
- 梳理 PostgreSQL 剩余能力边界；
- 跑最终相关测试集并准备中文提交说明。
