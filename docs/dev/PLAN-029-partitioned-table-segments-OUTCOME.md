# PLAN-029-partitioned-table-segments-OUTCOME

## 实施摘要

本轮按 `docs/design/PLAN-030-partitioned-sqlite-final-design.md` 完成第一阶段重构闭环。

已实现：

- 新核心契约名 `TableContract`。
- 旧 `TableSpec` 降级为 `TableContract` 的过渡别名。
- 新增 `Segment.rows(...)`。
- 新增 `Partition.range(...)`、`Partition.duration(...)`、`Partition.calendar(...)` 工厂。
- 新增 runtime `RangePartition`。
- `Database.table(...)` 支持直接声明：
  - `pk`
  - `columns`
  - `partition`
  - `segment`
  - `order_key`
  - `conflict`
- `Table.insert_all(rows)` 可直接使用表契约，不再要求每次写入传 `pk` / `partition`。
- 无 `partition` 但有 `segment` 时，使用默认 logical partition `__all__`。
- 一个 logical partition 可以拥有多个 physical segments。
- segment manifest 增加通用列统计 `stats_json`。
- `Table.explain_select(...)` 暴露 select 查询计划。
- 查询执行两层裁剪：
  - logical partition pruning
  - segment stats pruning
- 晚到数据写入新 active/delta segment，不回写旧 sealed segment。
- 新契约 metadata 写入 `table_contract`，无时序表不写 `"time": null`。
- 主线示例从 `TableSpec/time/query()` 切换为 `Database.table(..., partition=..., segment=...)`、`insert_all()`、`select(where=...)`、`explain_select(...)`。
- `timeseries()` 示例保留为 helper 版本。

## 关键取舍

### 仍保留的旧对象

`TimeColumn`、`TableSpec`、`query(start/end/date)`、`timeseries()` 暂未删除。

原因：

- 本轮目标是先让新核心路径可运行并可测试。
- 旧 timeseries helper 仍有测试覆盖和业务价值。
- `TableSpec` 已不再是新主路径，当前只是 `TableContract` 的过渡别名。

后续如果要彻底清理旧代码，应单独做第二轮删除和文档迁移。

### append-only segment 语义

`Segment.rows(...)` 下，append 模式不主动跨 sealed segments 查重。

这与最终设计一致：

- append 表优先写入速度。
- 跨 segment 主键唯一性需要后续全局 pk index。
- upsert/keyed 表应等 tombstone 和 pk index 模型准备好再完整开放。

## 新增测试

新增文件：

- `tests/sqlite/test_partitioned_table_contract.py`
- `examples/06_row_segment_late_data.py`

覆盖：

- range 分区不需要 `time` key。
- calendar 分区不需要 `time` key。
- `Database.table(...).insert_all(...)` 主路径。
- `explain_select(...)` 的 logical partition pruning。
- 纯 row segment 使用 `__all__`。
- 晚到数据写入新 segment。
- 查询扫描重叠 segment 并跳过无关 segment。
- `segment_skip_reasons` 使用 `pruned_by_segment_stats`。

同时更新了旧 timeseries 测试中的断言，使其符合新设计：

- `candidate_partitions` 表示裁剪后的候选 logical partitions。
- segment skip reason 从 `pruned_by_segment_time_range` 改为 `pruned_by_segment_stats`。
- append-only segment 允许跨 sealed segments 重复主键。

更新示例和文档：

- `examples/01_quickstart_table.py`
- `examples/02_calendar_events_table.py`
- `examples/03_buffered_ingest_and_query_plan.py`
- `examples/05_partition_segments_smoke.py`
- `examples/README.md`
- `README.md`
- `iosql/utils/mysqlite/README.md`

## 验证结果

已通过：

```bash
python examples/01_quickstart_table.py
python examples/02_calendar_events_table.py
python examples/03_buffered_ingest_and_query_plan.py
python examples/05_partition_segments_smoke.py
python examples/06_row_segment_late_data.py
python examples/01_quickstart_timeseries.py
python examples/02_calendar_events.py
python examples/04_layout_report_and_repartition.py
```

结果：全部成功执行。

已通过：

```bash
python -m pytest tests/sqlite/test_partitioned_table_contract.py -q
```

结果：

```text
3 passed
```

已通过：

```bash
python -m pytest tests/query tests/sqlite -q
```

结果：

```text
51 passed, 12 skipped
```

已通过：

```bash
python -m pytest tests/utils -q
```

结果：

```text
43 passed, 1 skipped
```

已通过：

```bash
python -m pytest tests/test_partition.py -q
```

结果：

```text
30 passed
```

已通过：

```bash
python -m pytest tests/test_postgresql.py -q
```

结果：

```text
1 skipped
```

已通过：

```bash
python -m pytest tests/test_partition_integration.py -q -k 'not large'
```

结果：

```text
39 passed, 3 deselected
```

已通过：

```bash
python -m compileall iosql -q
```

结果：无编译错误。

## 未完整跑完的验证

尝试运行：

```bash
python -m pytest -q
```

运行到：

```text
122 passed, 12 skipped
```

后因历史大数据/长耗时测试持续运行超过 3 分钟被手动中断。

单独运行：

```bash
python -m pytest tests/test_partition_integration.py -q
```

运行到：

```text
41 passed
```

后卡在该文件的大数据/连接池相关路径，被手动中断。

该文件排除 `large` 后已通过。未完成的是历史压力/大数据用例，不是本轮新增核心设计测试。

## 后续建议

后续阶段建议按 `PLAN-030` 继续：

1. 完整删除或下沉核心层 `TimeColumn`。
2. 删除或迁移旧 `TableSpec` 测试和文档。
3. 实现 compaction。
4. 实现全局 pk index 和 tombstone。
5. 支持 keyed/upsert row segment。
6. 支持 bytes segment。
7. 支持 hash/value partition。
