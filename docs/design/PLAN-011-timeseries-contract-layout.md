# PLAN-011-timeseries-contract-layout

## 为什么做这个计划

当前主线已经有 `DurationPartition` / `CalendarPartition` 对象模型，能把数据按时间分区并持久化分区元信息。但用户仍需要在写入时理解和传入分区对象，查询也仍暴露底层 `where` AST。目标用户是金融时序数据使用者，他们关心的是表的业务契约、时间范围查询、批量幂等写入和物理文件规模，而不是每次操作都处理分区。

branch2 的核心价值是上层契约设计：`TableSpec`、场景化 `query()`、`WriteResult`、`ChunkManifest` 和安全删除。主线当前的分区对象比 branch2 的 legacy 字符串分区更新，所以本计划只迁移契约思想，不迁移旧字符串分区实现。

## 目标

1. 新增表级时序契约，让用户通过 `Database.timeseries()` 或 `Database.table(..., spec=...)` 定义表。
2. 新增统一写入接口 `Table.write()`，默认使用表级主键、分区策略和 schema 策略。
3. 新增场景化查询 `Table.query(...).to_list()` 和 `query.explain()`，支持金融常见的 `symbols/start/end/date`。
4. 新增物理布局契约，默认单分区文件目标行数为 40 万，软范围为 30 到 50 万。
5. 新增布局报告，标出过大分区、过小分区和文件数量风险。
6. 新增生成式重分区接口：不改动旧库，读取旧分区并写入目标库的新数据文件。
7. 新增内存缓冲写入入口，作为 WAL-like 写入聚合的第一阶段。
8. 修复非 UTC `CalendarPartition.get_range()` 使用 UTC 日边界的问题，避免剪枝漏查。

## 非目标

1. 本计划不做原地改写、删除、移动或合并旧分区文件。
2. 本计划不实现持久化 WAL 日志文件；先提供内存缓冲写入对象。
3. 本计划不实现自动根据历史密度动态改变线上表的当前分区策略。
4. 本计划不引入交易日历、半日市、夜盘等金融日历能力。
5. 本计划不做全量 SQL 参数化治理。

## 用户接口

```python
from iosql import Database

db = Database("sqlite://data/market.iosql")

ticks = db.timeseries(
    "ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    columns={
        "symbol": "text",
        "ts": "integer",
        "price": "float",
    },
    partition_every="1day",
    target_rows_per_file=400_000,
)

ticks.write(rows, mode="upsert")

rows = ticks.query(
    symbols=["AAPL", "MSFT"],
    start=1705276800000,
    end=1705363200000,
).to_list()

report = ticks.layout_report()
```

当发现分区过大或过小时，用户生成一个新库：

```python
new_spec = ticks.spec().with_partition(every="1hour")
result = ticks.repartition_to(
    "sqlite://data/market-repacked.iosql",
    spec=new_spec,
)
```

这个接口只读旧文件并写入新目标，不改变旧库。

## 设计

### 表契约

新增 `iosql/spec.py`：

- `TimeColumn`：表达时间列、单位和 timezone。
- `PartitionSpec`：把用户的 `every="1day"` 编译为当前主线的 `DurationPartition` 或 `CalendarPartition` 对象。
- `PhysicalLayout`：表达目标文件行数，默认 `target=400000`、`min=300000`、`max=500000`。
- `TableSpec`：包含主键、时间列、分区、字段类型、schema 策略、symbol 字段和物理布局。
- `WriteResult` / `QueryPlan` / `LayoutReport` / `RepartitionResult`：用户可序列化结果对象。

### 物理布局策略

本阶段不在写入时强制拆文件，因为当前分区 key 是纯时间函数，在线改策略会改变后续文件命名和历史布局。更稳妥的方式是：

1. `write()` 在结果中报告触碰分区和布局 warning。
2. `layout_report()` 统计每个分区行数。
3. `repartition_to()` 读旧分区、按新 `TableSpec` 写入新目标库。

这样可以先让用户掌握物理层状态，并通过离线整理生成新数据文件。后续可以在 manifest 稳定后增加文件级原子替换和多版本目录。

### WAL-like 缓冲

新增 `Table.write_buffer(max_rows=...)` 返回内存缓冲对象。它会累计行数，达到阈值后调用 `write()` 刷入正式分区。第一阶段不提供崩溃恢复；后续可把 buffer 文件化，形成真正的 ingest log。

### 查询

`Table.query()` 负责把用户意图编译成旧 `select()` 可执行的条件：

- `symbols` -> symbol column 的 `=` 或 `in`
- `start/end` -> time column 的范围条件
- `date` -> date 表等值或 timestamp 表自然日范围
- `where` -> 额外条件，与上述条件 AND 组合

`query.explain()` 返回候选分区、扫描分区、跳过分区和 Python 层排序/limit 标记。

## 实施步骤

1. 新增 `iosql/spec.py` 和错误类型。
2. 扩展 `Database.table()` / `Database.timeseries()`。
3. 扩展 sqlite `Table`：`spec/use_spec/write/query/layout_report/repartition_to/write_buffer`。
4. 修复 `CalendarPartition.get_range()` 时区边界。
5. 修复 `Where(list)`、`op="="` 剪枝、`in` SQL 和空库探测。
6. 增加测试。
7. 运行目标测试和全量测试。

## 风险

1. Python 层全局排序和布局统计需要读取分区数据，超大数据下应谨慎使用。
2. `repartition_to()` 第一阶段走行级重写，安全但不是最高性能。
3. 内存缓冲不是持久化 WAL，进程崩溃会丢失未 flush 行。
4. 当前默认时间分区仍由用户或 `timeseries()` 的 `partition_every` 决定，自动根据密度调参需要后续引入采样和推荐器。
