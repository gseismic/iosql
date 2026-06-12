# PLAN-011-timeseries-contract-layout OUTCOME

## 实施结论

已完成第一阶段时序表契约和物理布局能力。当前用户可以通过 `Database.timeseries()` 定义金融时序表，通过 `Table.write()` 统一写入，通过 `Table.query(...).to_list()` 按标的和时间范围查询，并通过 `query.explain()` 查看分区扫描计划。

本阶段同时新增布局报告和生成式重分区接口：系统不改动旧数据文件，只读取旧分区并写入目标库的新数据文件。默认物理目标为单分区文件 40 万行，软范围 30 到 50 万行。

## 已完成变更

### 1. 表级契约

新增 `iosql/spec.py`：

- `TimeColumn`
- `PartitionSpec`
- `PhysicalLayout`
- `TableSpec`
- `WriteResult`
- `QuerySpec`
- `QueryPlan`
- `PartitionStats`
- `LayoutReport`
- `RepartitionResult`

`PartitionSpec` 编译到当前主线的 `DurationPartition` / `CalendarPartition`，没有迁回 branch2 的 legacy 字符串分区体系。

### 2. 数据库入口

`Database` 新增：

- `table(name, spec=None)`
- `timeseries(...)`

`table_names()` 改为直接读取数据库目录，不再通过 `__dummy_table` 创建副作用目录。

### 3. SQLite 表接口

`Table` 新增：

- `use_spec(spec)`
- `spec()`
- `write(rows, mode="upsert")`
- `query(...).to_list()`
- `query(...).explain()`
- `layout_report()`
- `repartition_to(target_uri, spec=...)`
- `write_buffer(max_rows=...)`
- `truncate(confirm=True)`

`write()` 会返回 `WriteResult`，包含行数、触碰分区、新建分区、schema 变化和物理布局 warning。

### 4. 物理布局能力

新增 `PhysicalLayout`，默认：

- `target_rows_per_file=400000`
- `min_rows_per_file=300000`
- `max_rows_per_file=500000`

`layout_report()` 会统计每个分区文件的行数、时间范围、是否超过上限、是否过小，并给出重分区建议。

`repartition_to()` 会把旧表数据按新 `TableSpec` 写入目标库，不移动、不删除、不覆盖旧库文件。

### 5. WAL-like 缓冲写入

新增 `write_buffer(max_rows=...)`，提供内存缓冲写入对象。达到阈值自动 flush，退出上下文时 flush 剩余数据。

这是 WAL-like 聚合写入的第一阶段，不提供崩溃恢复；后续可以扩展为持久化 ingest log。

### 6. branch2 思想迁移

已迁移并适配：

- 表级契约思想。
- 场景化查询和 explain。
- 结构化结果对象。
- 安全 `truncate(confirm=True)`。
- `Where(list)` 条件组合。
- `op="="` 范围剪枝。
- `IN (...)` SQL。
- 空库探测不产生副作用。

没有迁移：

- 旧字符串 partition 作为主路径。
- branch2 中与当前 `Partition` 抽象基类冲突的 `Partition` 工厂名。
- 直接文件替换式 chunk import。

### 7. 正确性修复

- 修复 `CalendarPartition.get_range()`：非 UTC 日历分区按配置 timezone 解释分区 key，再转 UTC 边界。
- 扩展 `DurationPartition` 常见复数 alias。
- 修复分区剪枝对右开边界的过度扫描。
- 修复 `iosql/sqlite/backup/ref.py` 的历史语法错误，保留为可编译占位模块。

## 新增测试

新增 `tests/sqlite/test_timeseries_contract.py`，覆盖：

1. `Database.timeseries()` + `Table.write()` + `Table.query()`。
2. `query.explain()` 的候选、扫描、跳过分区。
3. `TableSpec` 持久化后重新打开读取。
4. 过大分区通过 `layout_report()` 标记。
5. `repartition_to()` 生成新库且旧库分区不变。
6. `write_buffer()` 达到阈值自动 flush。
7. `truncate(confirm=True)` 安全确认。
8. 非 UTC `CalendarPartition.get_range()` 边界。

## 验证结果

- `python -m pytest tests/sqlite/test_timeseries_contract.py -q`：通过，`7 passed`。
- `python -m pytest tests/test_partition.py tests/test_partition_integration.py tests/sqlite/test_timeseries_contract.py tests/sqlite/test_insert.py tests/sqlite/test_classic_use.py -q`：通过，`81 passed`。
- `python -m pytest tests -q`：通过，`115 passed, 14 skipped`。
- `python -m compileall -q iosql`：通过。

测试过程中 pytest cache 因仓库上级目录权限产生 warning，不影响测试结果。

## 剩余风险

1. `layout_report()` 当前会读取分区行数据来计算时间范围，超大分区下成本较高。
2. `repartition_to()` 走行级读写，安全但不是最高性能；后续可基于 manifest 做文件级原子生成。
3. `write_buffer()` 是内存缓冲，不是持久化 WAL；进程崩溃会丢失未 flush 数据。
4. 当前不会在线自动改变已有表的分区策略；分区过大/过小时通过报告和离线重分区处理。
5. 尚未实现交易日历、交易时段、夜盘等金融市场特有分区策略。
