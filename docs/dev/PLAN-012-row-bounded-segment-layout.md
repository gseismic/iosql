# PLAN-012-row-bounded-segment-layout

## 下一高价值目标

引入“逻辑时间分区 + 物理行数分段”的布局层，保证物理层单个 SQLite 分区文件的行数可被控制在目标范围内。

这一步优先级高于持久化 WAL。原因是 WAL-like 缓冲只能把写入聚合起来，减少过早生成小文件；但如果某个交易日或某个时间桶本身有几百万行，最终 flush 仍会写进同一个时间分区文件。用户的核心目标是“物理层单个文件的表中数据不能太多”，所以必须先把“一个逻辑时间范围可以对应多个物理 segment 文件”作为底层能力。

## 背景

`PLAN-011` 已经完成：

- 表级 `TableSpec`。
- 统一 `write()` / `query()`。
- `PhysicalLayout`，默认 40 万行目标、30 到 50 万软范围。
- `layout_report()`。
- `repartition_to()`，不动旧文件、生成目标库新文件。
- 内存 `write_buffer()`。

当前缺口：

1. `PhysicalLayout` 仍是报告和建议，不能硬性防止超大文件。
2. 当前物理文件名仍基本等同于时间分区 key，例如 `1705276800000.sqlite3`。
3. 一个时间桶只能有一个文件，导致高频 tick 在单日或单小时内仍可能超过 50 万行。
4. `write_buffer()` 不是持久化 WAL，且 flush 后仍受单文件时间桶限制。
5. `layout_report()` 当前需要读取分区行数据，缺少常驻 manifest。

## 目标

1. 新增物理 segment manifest，区分逻辑时间分区和物理文件。
2. 新写入路径支持按 `PhysicalLayout.max_rows_per_file` 追加或创建 segment 文件。
3. 查询路径按时间范围先剪枝逻辑分区，再扫描命中的物理 segments。
4. 旧单文件分区作为 legacy segment 继续可读，不强制迁移。
5. `layout_report()` 优先从 manifest 读取行数和时间范围，避免全量扫描文件。
6. `repartition_to()` 能生成 row-bounded segment 布局，不动源库。
7. 为后续持久化 WAL 和 chunk sync 留出 segment 级 manifest 字段。

## 非目标

1. 不删除、不移动、不原地改写旧分区文件。
2. 不实现持久化 WAL；该能力放到 `PLAN-013`。
3. 不引入交易日历或 session calendar。
4. 不做跨 segment 的并行查询优化。
5. 不做文件级原子替换导入。

## 设计

### 逻辑分区和物理 segment

逻辑分区仍来自当前 `Partition` 对象：

```text
logical_part = DurationPartition("1day").get_key(row)
```

物理文件改为 segment：

```text
data/
  1705276800000__000000.sqlite3
  1705276800000__000001.sqlite3
  1705276800000__000002.sqlite3
```

每个 segment 记录行数、时间范围和文件路径。

### manifest 表

在 table meta db 中新增表，例如 `_iosql_segments`：

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

字段说明：

- `logical_part`：时间分区 key，用于时间剪枝。
- `segment_id`：同一逻辑分区内递增编号。
- `part_key`：实际物理文件 key，兼容 `_get_part_dbfile(part_key)`。
- `row_count`：segment 当前行数。
- `sealed`：达到上限后封存，不再追加。
- `generation`：后续离线整理生成新布局时区分代际。

### 写入策略

`write()` 对每个逻辑分区内的数据批次：

1. 找到未 sealed 的最后一个 segment。
2. 如果当前 segment 行数 + batch 行数不超过 `max_rows_per_file`，追加。
3. 如果超过上限，按 `target_rows_per_file` 或 `max_rows_per_file` 切 batch，创建新 segment。
4. 每次写入后更新 segment manifest。

这能让高频数据自动拆成多个物理文件，不要求用户调整时间分区。

### 查询策略

查询仍从用户角度按时间范围：

1. `Where` / `QuerySpec` 生成时间范围。
2. 用逻辑分区范围剪枝。
3. 再用 segment manifest 的 `time_min/time_max` 二次剪枝。
4. 扫描命中的 segment 文件。

`query.explain()` 增加：

- `candidate_segments`
- `scanned_segments`
- `skipped_segments`
- `segment_skip_reasons`

### 旧文件兼容

已有文件仍按旧 `table_parts()` 读：

- 若 segment manifest 不存在，当前行为不变。
- 首次 `layout_report()` 可把旧 `part` 映射为 legacy segment，但不改旧文件。
- `repartition_to()` 可把旧单文件布局写成新 segment 布局。

### 与 WAL-like 缓冲的关系

本计划完成后，持久化 WAL 的 flush 目标就不再是单个时间分区文件，而是 segment writer。这样 WAL 后续能解决崩溃恢复和批量提交，segment layout 负责物理文件规模约束。

## 用户接口变化

第一阶段用户接口可以保持不变：

```python
ticks = db.timeseries(
    "ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    partition_every="1day",
    target_rows_per_file=400_000,
    max_rows_per_file=500_000,
)

ticks.write(rows)
report = ticks.layout_report()
```

新增可选 inspect 接口：

```python
segments = ticks.segments()
plan = ticks.query(start=..., end=...).explain()
```

## 实施步骤

1. 新增 segment 数据对象：
   - `SegmentInfo`
   - `SegmentLayoutReport` 或扩展 `PartitionStats`
2. 新增 sqlite meta 表读写：
   - `_table_segment_infos()`
   - `_upsert_segment_info()`
   - `_active_segment_for(logical_part)`
   - `_next_segment_key(logical_part)`
3. 改写新 `Table.write()` 路径：
   - 只对 `TableSpec.physical_layout` 启用 segment layout。
   - 旧 `insert_all()` 兼容路径保持原行为。
4. 改写 `query()` / `select()` 内部扫描：
   - 如果 manifest 存在，用 segment list。
   - 否则用旧 `table_parts()`。
5. 扩展 `layout_report()`：
   - 优先使用 segment manifest。
   - 旧布局 fallback 扫描文件。
6. 扩展 `repartition_to()`：
   - 目标表使用 segment writer。
   - 源表旧/新布局都可读取。
7. 补测试：
   - 单逻辑分区内超过 max rows 自动拆多个 segment。
   - 查询时间范围命中多个 segment。
   - explain 展示 segment 级扫描。
   - 旧布局仍可读。
   - `repartition_to()` 从旧布局生成新 segment 布局。

## 验证标准

1. 用户无需传分区对象即可写入大批数据。
2. 单个 segment 文件行数不超过 `max_rows_per_file`，除非单行或事务边界导致明确例外。
3. 查询按时间范围返回正确结果。
4. `query.explain()` 能显示逻辑分区和物理 segment。
5. 旧 `insert_all/select/table_parts` 测试继续通过。
6. `python -m pytest tests -q` 通过。
7. `python -m compileall -q iosql` 通过。

## 后续计划

`PLAN-013` 建议做持久化 WAL / ingest log：

- `table.ingest(rows)` 先写入 append-only log。
- 达到行数、时间或手动 checkpoint 后 flush 到 segment writer。
- 崩溃后可 replay。
- 可做批量去重、幂等 upsert 和 schema evolve 检查。

这个顺序能保证 WAL flush 的目标已经具备 row-bounded 物理布局能力。
