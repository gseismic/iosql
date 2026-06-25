# PLAN-045 流式查询与大结果集导出 OUTCOME

## 结果

已完成 SQLite 表的用户层流式查询和 CLI 批处理导出。

本轮解决的问题是：金融 K 线、tick、逐笔成交、orderbook、事件数据等大结果集不再必须通过 `select()` / `query().to_list()` / CLI `export` 一次性加载到内存后再消费。

## 新增 Python API

### `table.iter_select(...)`

```python
for row in table.iter_select(
    "*",
    where={"field": "ts", "op": ">=", "value": start},
    batch_size=50_000,
    to_dict=True,
):
    process(row)
```

### `table.iter_select_batches(...)`

```python
for batch in table.iter_select_batches(
    "symbol,ts,price",
    where={"field": "ts", "op": "between_and", "value": [start, end]},
    batch_size=50_000,
    to_dict=True,
):
    process(batch)
```

行为：

- `batch_size` 必须为正整数。
- `limit` 跨 batch 生效。
- 无全局 `order_by` 时按分区/segment 分页读取。
- 有 `order_by` 时，为保证跨 segment 全局排序正确性，仍复用 `select()` 合并排序后再分批输出。
- `select()`、`select_all()` 的返回行为不变。

### `query(...).iter_batches(...)`

```python
for batch in ticks.query(start=start, end=end).iter_batches(batch_size=50_000):
    process(batch)
```

`query(...).iter_rows(batch_size=...)` 也已改为走 `iter_select(...)`，不再直接 `iter(to_list())`。

## CLI 改动

`iosql export` 新增 `--batch-size`，并改为批处理写文件：

```bash
iosql export \
  -s sqlite://db/market.iosql \
  -t ticks \
  -o ticks.jsonl \
  --where '{"field":"ts","op":">=","value":1735689600000}' \
  --batch-size 50000
```

格式行为：

- JSONL：逐行写入；
- CSV：首批写 header，后续追加行；
- JSON：流式写数组元素，不先构造完整 rows 列表；
- 输出 summary 新增 `batch_size`。

CLI `query` 保持面向终端排查的小结果集行为，不改为流式终端输出。

## 文件变更

### 核心

- `iosql/engines/sqlite/table.py`
  - `_TableQuery.iter_rows(batch_size=...)`
  - `_TableQuery.iter_batches(batch_size=...)`
  - `Table.iter_select(...)`
  - `Table.iter_select_batches(...)`
  - `_iter_select_raw_batches(...)`
  - `_select_queries_for_where(...)`
  - `_iter_part_query_batches(...)`
  - `_normalize_batch_size(...)`

### CLI

- `iosql/cli.py`
  - 新增 `_write_row_batches(...)`
  - `export` 增加 `--batch-size`
  - `export` 改为消费 `iter_select_batches(...)`

### 测试

- `tests/engines/sqlite/test_partitioned_table_contract.py`
  - `iter_select_batches` 批大小、limit、字段投影、`order_by` 兼容。
- `tests/engines/sqlite/test_timeseries_contract.py`
  - `query(...).iter_batches(...)` 与 `to_list()` 结果一致。
  - `query(...).iter_rows(batch_size=...)` 与 `to_list()` 结果一致。
- `tests/test_cli.py`
  - `export --batch-size 1` 覆盖 JSONL、CSV、JSON 三种输出。
  - 既有 export/import CLI 回归保持通过。

### 文档 / skill

- `README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/api-reference.md`
- `skills/iosql-usage/references/usage-patterns.md`

## 验证

已执行：

```bash
python -m compileall iosql tests -q
python -m pytest tests/engines/sqlite/test_partitioned_table_contract.py::test_iter_select_batches_streams_rows_and_honors_limit tests/engines/sqlite/test_timeseries_contract.py::test_timeseries_query_iter_batches_matches_to_list tests/test_cli.py::test_cli_export_streams_jsonl_csv_and_json_batches tests/test_cli.py::test_cli_explain_query_export_and_import -q
python -m pytest tests/engines/sqlite/test_partitioned_table_contract.py tests/engines/sqlite/test_timeseries_contract.py tests/test_cli.py tests/test_migration.py -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/engines/postgresql tests/core/query tests/utils/mysqlite -q
python -m pytest tests/test_partition_integration.py -q -k 'not test_upsert_large_data'
git diff --check
```

结果：

- `4 passed in 1.50s`
- `91 passed in 24.57s`
- `237 passed, 13 skipped in 26.96s`
- `41 passed, 1 deselected in 21.92s`
- `git diff --check` 通过

## 边界

1. 带全局 `order_by` 的跨 segment 查询仍需要 Python 层合并排序；本轮不实现外部归并排序或磁盘临时排序。
2. CSV 流式导出的 header 基于首批返回行；正常契约表列稳定，动态列极端场景仍建议显式 `--fields`。
3. CLI `query` 仍是终端排查入口，不适合无限大输出；大结果集使用 `export --batch-size`。
4. PostgreSQL 引擎未实现 server-side cursor 流式查询。
5. CLI `import` 仍会先读取完整输入文件；这是写入侧下一处明显瓶颈。

## 下一步高价值目标

建议下一步做 **PLAN-046：流式 import 大文件与批量写入管线**。

原因：

1. 本轮已解决大结果集导出，但 `iosql import` 仍通过 `_read_rows()` 一次性读取 JSON/JSONL/CSV。
2. 金融数据落库常见来源是 vendor CSV、JSONL、历史回放文件和离线回补包，单文件可能是数 GB。
3. Python API 已有 `write_buffer()`，CLI 需要把文件读取、格式解析、批量写入连成流式管线。
4. 完成后，iosql 的 CLI 数据进出链路会从“可用工具”升级到“海量文件可操作工具”。

建议范围：

- `iter_read_rows(file, format_)`
- CLI `import --batch-size` 真正边读边写
- JSONL/CSV 流式解析
- JSON 数组可保持兼容但提示大文件推荐 JSONL/CSV
- 增加导入大文件不会调用 `_read_rows()` 的回归测试
