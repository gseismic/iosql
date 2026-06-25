# PLAN-046 流式 import 大文件与批量写入管线 OUTCOME

## 结果

已完成 CLI `import` 的流式读取和批量写入管线。

本轮解决的问题是：金融 vendor CSV、tick JSONL、orderbook 回放文件、历史回补包等大文件不再需要被 `iosql import` 一次性读入内存后再写入。

## 实现内容

### 流式读取 helper

`iosql/cli.py` 新增：

- `_iter_read_rows(path, format_)`
- `_iter_row_batches(path, format_, batch_size)`
- `_coerce_import_batch(batch, table_spec)`
- `_coerce_import_value(value, type_, column)`

行为：

- JSONL：逐行读取、逐行解析；
- CSV：使用 `csv.DictReader` 按行读取；
- JSON：保持对象或对象数组兼容，仍使用 `json.load`；
- `_read_rows(...)` 保留为兼容 helper，但基于 `_iter_read_rows(...)` 实现；
- CSV 字段按表契约 `columns` 对 integer/float/bool 做基础类型转换；
- 空字符串转换为 `None`。

### CLI import

`iosql import` 现在直接消费 `_iter_row_batches(...)`：

```bash
iosql import \
  -s sqlite://db/market.iosql \
  -t ticks \
  -f ticks.jsonl \
  --format jsonl \
  --batch-size 50000 \
  --mode append
```

输出 summary：

```json
{
  "rows_seen": 25000000,
  "rows_written": 25000000,
  "mode": "append",
  "batch_size": 50000
}
```

## 测试

`tests/test_cli.py` 新增 `test_cli_import_streams_jsonl_csv_and_keeps_json_compat`：

- monkeypatch `_read_rows` 抛错，证明 `import` 不再依赖全量读取；
- JSONL 使用 `--batch-size 2` 多批导入；
- CSV 使用 `--batch-size 1` 多批导入；
- JSON 数组继续兼容；
- CSV `ts` / `price` 按表契约转换后可写入 duration partition 表；
- `--batch-size 0` 正确拒绝。

## 文档 / skill

已更新：

- `README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/usage-patterns.md`

新增说明：

- 大文件导入优先用 JSONL/CSV 和 `iosql import --batch-size`；
- JSON 数组兼容但不推荐用于数 GB 文件；
- CSV 导入会按契约列类型做基础转换。

## 验证

已执行：

```bash
python -m compileall iosql tests -q
python -m pytest tests/test_cli.py -q
python -m pytest tests/engines/sqlite/test_partitioned_table_contract.py tests/engines/sqlite/test_timeseries_contract.py tests/test_cli.py tests/test_migration.py -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/engines/postgresql tests/core/query tests/utils/mysqlite -q
python -m pytest tests/test_partition_integration.py -q -k 'not test_upsert_large_data'
git diff --check
```

结果：

- `23 passed in 6.80s`
- `92 passed in 15.57s`
- `238 passed, 13 skipped in 22.67s`
- `41 passed, 1 deselected in 13.52s`
- `git diff --check` 通过

## 边界

1. JSON 数组仍不是流式解析；大文件推荐 JSONL/CSV。
2. CSV 类型转换只覆盖 integer/float/bool 和空字符串到 `None`，复杂日期/时区转换仍由用户预处理或通过表设计表达。
3. 目标表仍必须已有 `TableSpec/TableContract`。
4. 不实现自动建表导入。
5. 不改变 `mode=append/ignore/upsert/replace` 语义。

## 下一步高价值目标

建议下一步做 **PLAN-047：PostgreSQL 迁移验证与目标侧查询能力增强**。

原因：

1. SQLite 本地大文件导入、导出、compaction、cleanup 的运维链路已经明显完整。
2. 金融海量数据最终常见形态是本地 SQLite 采集/回测 + PostgreSQL 中心化服务或共享查询。
3. `PLAN-043` 已提供 SQLite 到 PostgreSQL 迁移和基础写入，但目标侧查询能力、row-count 校验、迁移 verify/resume 还偏弱。
4. 下一步补齐 PostgreSQL 迁移验证，可以让 iosql 从“能迁移”提升到“迁移后可证明一致、可用于服务端查询”。

建议范围：

- `iosql migrate --verify row-count`
- `Database.migrate_to(..., verify="row-count")`
- PostgreSQL `select()` / `iter_select_batches()` 基础查询能力
- 迁移结果记录源/目标行数
- 可选 `--dry-run` 估算表、行数、批次数
