# PLAN-038-write-hotpath-second-stage

## 背景

`PLAN-037` 已完成第一轮写入性能优化并提交。对提交后的代码重新执行 50 万行 `cProfile`，发现两个剩余热点：

1. `insert_all()` 先通过 `contract.parts_for(rows)` 计算 `partitions_touched`，随后 `do_insert_all()` 再逐行调用 `partition.get_key()` 完成分组。同一批数据的分区键被计算两次：
   - 日线 50 万行：额外约 0.44 秒。
   - orderbook 50 万行：额外约 0.43 秒。
2. append-only segment 默认对全部列计算 min/max。orderbook 50 万行、7 列的 `_batch_column_stats()` 约 1.40 秒，其中大部分查询通常只需要时间、序列等裁剪字段。

## 目标

- 每批数据只计算一次分区键。
- 允许调用方限制 segment 通用统计列，降低高频宽表写入 CPU 和临时对象成本。
- 默认行为保持兼容，未配置 `stats_columns` 时仍统计全部列。
- 不允许配置导致时间、session、partition 或 order key 的必要裁剪统计丢失。

## 设计

### 1. 复用写入分组结果

`do_insert_all()` 在完成分组后返回实际触达的 logical partition：

```python
partitions_touched = self.do_insert_all(...)
```

`insert_all()` 不再提前调用 `contract.parts_for(rows)`。无分区、纯 segment 和普通 partition 的返回语义保持：

- 单文件：`[None]`
- 纯 segment：`["__all__"]`
- 有 partition：排序后的 partition key

### 2. Segment.stats_columns

新增：

```python
Segment.rows(
    max_rows=1_000_000,
    stats_columns=["ts", "sequence"],
)
```

语义：

- `None`：统计全部列，兼容当前行为。
- `[]`：只统计系统自动要求的列。
- 显式列表：统计列表列，并自动补充：
  - partition column
  - `time`
  - `order_key`
  - `session_column`

## 测试

- 验证 partition `get_key()` 每行只调用一次。
- 验证单文件、纯 segment、有 partition 的 `WriteResult.partitions_touched`。
- 验证 `stats_columns` 序列化和恢复。
- 验证 orderbook 只生成必要列 stats，查询裁剪仍正确。
- 未配置 `stats_columns` 时保留全部列 stats。

## 文档

- README 的 orderbook 示例配置 `stats_columns=["ts", "sequence"]`。
- `skills/iosql-usage` 与写入性能指南解释统计列对写入/查询的取舍。
