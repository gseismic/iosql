# PLAN-016-row-bounded-segment-layout

## 背景

系统 review 发现，当前 `PhysicalLayout` 已进入 `TableSpec`，但写入路径仍是“一个逻辑时间分区一个 SQLite 文件”。金融高频数据中，单日 tick、逐笔成交、orderbook 快照可能轻易超过几十万行；仅靠 `layout_report()` 告警不能阻止单文件继续膨胀。

已有 `PLAN-012` 给出了完整方向。本计划落地其中第一阶段能力：让时序表在同一逻辑分区内按行数拆分物理 segment 文件，并让查询路径能读取 segment manifest。

## 目标

1. 新增 segment manifest 元数据表，记录逻辑分区、物理文件 key、行数和时间范围。
2. `Table.write()` 在 `TableSpec.physical_layout.max_rows_per_file` 约束下，把单个逻辑分区拆成多个 segment。
3. `select()` / `query().to_list()` 能读取 segment 文件，同时旧的单文件分区继续可读。
4. `query().explain()` 增加 segment 级候选、扫描、跳过信息。
5. `layout_report()` 优先使用 manifest，报告 segment 粒度的物理文件状态。
6. 补充金融高频风格测试，验证单逻辑分区多 segment、查询正确性和 explain 信息。

## 非目标

1. 不实现持久化 WAL 或崩溃恢复。
2. 不做跨 segment 并行查询。
3. 不原地迁移旧分区文件。
4. 不改变用户现有 `timeseries()`、`write()`、`query()` 入口。
5. 不解决复杂更新导致的跨 segment 主键去重问题；本阶段按追加/写入批次保证物理文件行数。

## 设计

### 文件命名

同一逻辑分区下的物理 segment 使用稳定文件 key：

```text
data/
  1735689600000__000000.sqlite3
  1735689600000__000001.sqlite3
```

旧文件仍保持：

```text
data/
  1735689600000.sqlite3
```

### manifest

新增 `_iosql_segments` 表：

```text
table_name
logical_part
segment_id
part_key
path
row_count
time_min
time_max
created_at
updated_at
sealed
generation
```

`logical_part` 用于保留时间分区语义，`part_key` 对应实际 SQLite 文件名。查询时先用旧分区范围剪枝逻辑分区，再扫描命中的 segment 文件。

### 写入策略

1. 按逻辑分区分组。
2. 找到该逻辑分区最后一个未 sealed segment。
3. 如果剩余容量可容纳部分数据，先填满该 segment。
4. 超出部分按 `max_rows_per_file` 创建新 segment。
5. 每次写入后更新 manifest 行数和时间范围。

第一阶段为了保证行为可解释，`upsert`/`replace`/`ignore` 的 `rows_written` 仍按输入行数近似返回；manifest 行数以写入后实际 `COUNT(*)` 为准。

## 验证

1. 新增测试覆盖单逻辑分区拆成多个 segment。
2. 查询跨多个 segment 返回完整有序结果。
3. `query().explain()` 暴露 `candidate_segments`、`scanned_segments`、`skipped_segments`。
4. `layout_report()` 不再把已拆 segment 误报为 oversized。
5. 旧时序测试继续通过。
6. 运行：
   - `python -m pytest tests/sqlite/test_timeseries_contract.py -q`
   - `python -m compileall -q iosql tests/sqlite/test_timeseries_contract.py`
   - `git diff --check`
