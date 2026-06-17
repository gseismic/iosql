# PLAN-029-partitioned-table-segments

## 背景

最终设计已收敛到 `docs/design/PLAN-030-partitioned-sqlite-final-design.md`：

- iosql 核心是“原生 SQLite Table + 自动分区/分段”。
- 核心契约不再以 `time` 为中心。
- `Partition` 只负责按字段值做逻辑路由。
- `Segment` 只负责按物理大小滚动 SQLite 文件。
- 查询以 `select(where=...)` 和 `explain_select(...)` 为主。

用户明确允许破坏旧代码兼容性：没有用的旧代码和废代码可以删除。

## 本轮目标

实现最终设计的第一轮可运行闭环：

1. 契约模型
   - 引入 `TableContract` 作为核心表契约。
   - 契约字段包含 `pk`、`columns`、`partition`、`segment`、`order_key`、`conflict`。
   - 核心契约不包含 `time`、`TimeColumn`。
   - `TableSpec` 如短期保留，只作为兼容别名或历史对象，不作为主路径。

2. 分区 API
   - `Partition.range(column, every)`
   - `Partition.duration(column, every)`
   - `Partition.calendar(column, every="1day", timezone="UTC")`
   - 分区字段可以是任意可排序字段，不限时间。

3. 分段 API
   - `Segment.rows(max_rows=..., target_rows=None)`
   - 不传 `partition` 时，row segment 使用默认逻辑分区 `__all__`。
   - 允许一个 logical partition 下有多个 physical segments。

4. Table API
   - `Database.table(name, pk=..., columns=..., partition=..., segment=..., order_key=..., conflict=...)`
   - `Table.insert_all(rows)` 使用表契约，不要求每次写入传 `pk` / `partition`。
   - `Table.select(fields="*", where=..., order_by=..., limit=..., to_dict=...)` 是核心查询。
   - `Table.explain_select(...)` 暴露 logical partitions 和 physical segments 两层计划。

5. 查询裁剪
   - 有 `Partition` 时先做 logical partition pruning。
   - 有 `Segment` 时再通过 segment `stats_json` 做 segment pruning。
   - 晚到数据写入新 active/delta segment，不回写旧 sealed segment。
   - 当多个 segment 范围重叠且有 `order_by/limit` 时，计划标记需要全局处理。

6. 测试
   - range 分区不需要 `time`。
   - calendar 分区不需要 `time`。
   - 纯 row segment 可按行数滚动。
   - 后期补中间数据时，查询正确，`explain_select` 扫描多个重叠 segment 并跳过无关 segment。
   - `Database.table(...).insert_all(...)` 是主路径。

## 非目标

本轮不实现完整后台维护体系：

- 不实现持久化 ingest log。
- 不实现 compaction。
- 不实现全局 pk index。
- 不实现 tombstone。
- 不实现 bytes segment。
- 不实现 hash/value partition。

这些属于 `PLAN-030` 后续阶段。

## 旧代码处理原则

- 核心路径不再依赖 `TimeColumn`。
- 核心查询不再依赖 `query(start/end/date)`。
- 可变 `table.partition = ...` 不作为新主路径。
- 每次 `insert_all(..., pk=..., partition=...)` 不作为新主路径。
- 如果旧代码阻碍新模型，可以删除、降级或保留为薄兼容层，但不能影响新 API 设计。

## 验收

至少通过：

```bash
python -m pytest tests/sqlite/test_partitioned_table_contract.py
```

如果改动影响公共模块，再运行相关 SQLite/query 测试。

