# PLAN-026-table-api-redesign-reflection

> 后续修正：`PLAN-027-partitioned-sqlite-core-api.md` 进一步收敛了本设计的结论。核心 API 不再建议出现必填 `time` 参数，而应以 `partition` / `partition_by` 表达“原生 SQLite 表 + 自动分区”。`time` 只保留在 `timeseries()` helper 和时序便利查询中。

## 背景

用户给出的旧示例使用以下风格：

```python
db = Database(DB_URI)
partition = DurationPartition(interval_ms="1day", ts_col="timestamp")
db["metrics"].insert_all(rows, pk=["timestamp", "symbol"], partition=partition)
rows = db["metrics"].select("*", where={...})
```

或：

```python
table = db["quotes"]
table.partition = DurationPartition(interval_ms="1day", ts_col="trade_ts")
table.insert_all(data, pk=["trade_ts", "symbol", "venue"])
rows = table.select_all("*", where=None)
```

当前新示例使用：

```python
spec = TableSpec(
    primary_key=["symbol", "ts"],
    time=TimeColumn("ts", unit="ms", timezone="UTC"),
    partition=PartitionSpec.by_time("ts", every="1day"),
    columns={"symbol": "text", "ts": "integer", "price": "float"},
)
ticks = db.table("ticks", spec=spec)
ticks.write(rows)
ticks.query(start=..., end=...).to_list()
```

这个文档反思两种接口的取舍，并给出如果重新设计主 API 的建议。

## 结论先行

旧接口更轻、更接近 SQLite/字典写入直觉，适合示例和一次性脚本；当前 `TableSpec` 接口在正确性、持久化契约、查询语义、布局演进和金融时序能力上更稳。

如果重新设计，不建议回退到“每次写入传 `pk` 和 `partition`”作为主 API；建议把当前 `TableSpec` 能力包装成更轻的 `Database.table(..., primary_key=..., time=..., partition=...)` 或 `table.define(...)`，让用户直接使用原始 `Table`，但不要求一开始理解 `TableSpec`、`TimeColumn` 和 `PartitionSpec` 三个类。

推荐主线：

```python
db = Database("sqlite://examples/runtime/metrics.iosql")

metrics = db.table(
    "metrics",
    primary_key=["timestamp", "symbol"],
    time="timestamp",
    partition="1day",
    columns={"timestamp": "integer", "symbol": "text", "value": "float"},
)

metrics.write(rows)

rows = metrics.query(
    start=window_start,
    end=window_end,
    where={"field": "symbol", "op": "=", "value": "METRIC_A"},
).to_list()
```

`Database.timeseries()` 可以继续保留，但定位为兼容/快捷别名，而不是文档主入口。

## 两种接口对比

| 维度 | 旧接口：`insert_all(pk, partition)` | 当前接口：`TableSpec` + `write/query` | 判断 |
| --- | --- | --- | --- |
| 入门心智 | 直接：拿表、传分区、插入 | 显式但偏重：要理解 `TableSpec`、`TimeColumn`、`PartitionSpec` | 旧接口更易上手 |
| 表契约 | 分散在每次写入参数和可变 `table.partition` 上 | 表级契约持久化，重开后可恢复 | 当前接口更可靠 |
| 一致性 | 同一表不同批次可能传不同 pk/partition | 已存在表会校验 spec 兼容性 | 当前接口更安全 |
| 查询语义 | `select("*", where=...)` 只知道条件，不知道业务时间 | `query(start/end/date/session)` 知道时间列、单位和时区 | 当前接口更适合时序 |
| 分区裁剪 | 依赖 where AST 和 partition 对象推断 | 表契约直接参与 query plan | 当前接口更清晰 |
| 物理布局 | 只表达逻辑分区 | 可表达 `PhysicalLayout`、segment、layout report、repartition | 当前接口更可扩展 |
| 自定义能力 | 可直接传 `DurationPartition` / `CalendarPartition` 对象 | 需要经过 `PartitionSpec` 编译 | 旧接口有可借鉴之处 |
| API 命名 | `insert_all/select/select_all` 接近底层 SQL | `write/query/latest/asof/explain` 接近业务意图 | 两者应分层共存 |

## 旧接口可借鉴之处

### 1. `db["table"]` 的直接性

`db["metrics"]` 很直观，适合打开已有表或临时脚本。当前 `db.table("metrics", spec=...)` 更正式，但略重。

建议保留并强化分层：

```python
db["metrics"]          # 打开已有表或低层兼容入口
db.table("metrics")    # 主 API，支持声明或读取契约
```

### 2. 分区对象直接可见

旧示例里的：

```python
partition = DurationPartition(interval_ms="1day", ts_col="timestamp")
```

比 `PartitionSpec.by_time("timestamp", every="1day")` 更容易让用户理解“按 timestamp 每天切一个文件”。当前 `PartitionSpec` 的好处是可序列化、可持久化、可兼容 `TimeColumn`，但名字偏内部。

可借鉴做法：

```python
metrics = db.table(
    "metrics",
    primary_key=["timestamp", "symbol"],
    time="timestamp",
    partition=DurationPartition(interval_ms="1day", ts_col="timestamp"),
)
```

内部把 `DurationPartition` 规范化为 `PartitionSpec` 并持久化。

### 3. 一次性 bootstrap 很方便

旧接口允许一行完成首次写入：

```python
db["metrics"].insert_all(rows, pk=["timestamp", "symbol"], partition=partition)
```

这对小脚本非常顺手。当前 `write()` 要求表先有 `TableSpec`，对临时数据有摩擦。

可借鉴但需要加约束：

```python
db["metrics"].write(
    rows,
    primary_key=["timestamp", "symbol"],
    time="timestamp",
    partition="1day",
)
```

规则：仅当表还没有契约时允许从写入参数创建契约；如果表已存在契约，则这些参数必须兼容，否则报错。这样保留便捷性，又避免每次写入漂移。

### 4. `select()` 作为低层逃生口有价值

`query(start/end/date/session)` 不应覆盖所有 SQL 场景。保留 `select(fields, where=...)` 和 `select_all()` 作为低层兼容/逃生口是合理的，但教程主线应使用 `query()`。

### 5. 示例不被 `timeseries` 命名绑死

用户的直觉是“这是一个带时间列和分区的普通 Table”，不是必须先选择 “timeseries 类型”。这一点值得吸收。文档主入口应是 `db.table(...)`，`timeseries()` 只是少写配置的别名。

## 当前接口不应放弃的部分

### 1. 表级契约必须持久化

金融时序库不能只靠每次写入时传 `pk` 和 `partition`。原因：

- 重开数据库后必须知道主键、时间列、分区和 schema。
- 后续写入必须能拒绝不兼容主键或分区。
- 查询计划必须能知道时间列和分区范围。
- `latest/asof/session/layout_report/repartition_to` 都依赖表级契约。

### 2. `time` 和 `partition` 应继续分离

`time` 是业务时间语义，回答“`start/end/date/latest/asof` 应该使用哪一列、按什么单位和时区解释”。

`partition` 是物理布局，回答“数据落到哪些文件、查询时哪些文件可跳过”。

两者通常指向同一列，但不应合并成一个字段。例如：

- 表可以有 `time` 但不分区，用于小表或测试。
- 表可以按业务时间查询，但以后改变分区粒度。
- `date` 查询需要时区和单位信息，单纯 partition 对象不足以表达完整语义。

不过主 API 可以自动推导，减少重复：

```python
db.table(
    "metrics",
    primary_key=["timestamp", "symbol"],
    time="timestamp",
    partition="1day",
)
```

当 `partition` 是 `DurationPartition(ts_col="timestamp")` 且用户未显式传 `time` 时，也可以默认推导 `time=TimeColumn("timestamp", unit="ms")`，但文档仍应解释二者职责。

### 3. `write()` 比 `insert_all()` 更适合主 API

`insert_all()` 暗示纯追加，但当前主场景默认是幂等 upsert。`write(rows, mode="upsert")` 更准确：

- `mode="upsert"`：默认，主键相同则覆盖。
- `mode="append"`：严格追加，重复主键报错。
- `mode="ignore"`：重复主键跳过。
- `mode="replace"`：显式替换。

`insert_all()` 可以作为兼容/低层接口继续保留。

## 如果重新设计，推荐 API

### 1. 主入口：`Database.table()` 直接声明契约

```python
from iosql import Database

db = Database("sqlite://examples/runtime/metrics.iosql")

metrics = db.table(
    "metrics",
    primary_key=["timestamp", "symbol"],
    time="timestamp",
    partition="1day",
    columns={
        "timestamp": "integer",
        "symbol": "text",
        "value": "float",
    },
)

metrics.write(rows)
```

等价高级写法仍可保留：

```python
metrics = db.table("metrics", spec=TableSpec(...))
```

接口规则：

- `spec` 与快捷关键字不能同时传，避免冲突。
- 表已存在时，新传入契约必须兼容。
- `partition="1day"` 在 `time` 存在时默认表示 duration 分区。
- `partition={"kind": "calendar", "every": "1day"}` 或 `partition=CalendarPartition(...)` 表示日历分区。

### 2. 可链式声明：`table.define()`

对喜欢 `db["table"]` 的用户，可以提供：

```python
metrics = db["metrics"].define(
    primary_key=["timestamp", "symbol"],
    time="timestamp",
    partition="1day",
    columns={"timestamp": "integer", "symbol": "text", "value": "float"},
)

metrics.write(rows)
```

`define()` 本质上调用 `use_spec()`，但名字比 `use_spec()` 更面向用户。

### 3. 支持旧分区对象作为输入

```python
from iosql.partition import DurationPartition, CalendarPartition

metrics = db.table(
    "metrics",
    primary_key=["timestamp", "symbol"],
    partition=DurationPartition(interval_ms="1day", ts_col="timestamp"),
)

schedule = db.table(
    "schedule",
    primary_key=["event_time", "title"],
    time={"name": "event_time", "unit": "iso", "timezone": "UTC"},
    partition=CalendarPartition(fmt="YYYYMMDD", tz="UTC", ts_col="event_time"),
)
```

内部规则：

- `DurationPartition` 默认推导 `time_unit="ms"`。
- `CalendarPartition` 对 `YYYY-MM-DD` 字符串默认推导 `time_unit="date"`；如果要支持完整 ISO 时间字符串，应新增并明确 `time_unit="iso"` 或等价命名。
- 推导结果必须进入持久化 `TableSpec`，不要只保存在 runtime 对象上。

### 4. 写入时 bootstrap，但只允许首次定义

```python
db["metrics"].write(
    rows,
    primary_key=["timestamp", "symbol"],
    time="timestamp",
    partition="1day",
)
```

规则：

1. 表没有 `TableSpec`：从参数创建并持久化。
2. 表已有 `TableSpec`：参数为空则使用已有契约。
3. 表已有 `TableSpec` 且参数不为空：必须兼容，否则抛 `TableSpecConflict`。

这样吸收旧接口“一次写入就能建表”的优点，同时不牺牲契约一致性。

### 5. 查询：业务 API 为主，低层 select 为辅

主查询：

```python
rows = metrics.query(
    start=window_start,
    end=window_end,
    symbols="METRIC_A",
    columns=["timestamp", "symbol", "value"],
    order_by="timestamp",
).to_list()

plan = metrics.query(start=window_start, end=window_end).explain()
```

低层逃生：

```python
rows = metrics.select(
    "*",
    where={"field": "timestamp", "op": "between_and", "value": [start, end]},
    to_dict=True,
)
```

文档应明确：`query()` 是推荐主路径；`select()` 是兼容和高级逃生口。

### 6. `timeseries()` 的定位

保留：

```python
ticks = db.timeseries(
    "ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    partition_every="1day",
)
```

但文档中说明它等价于：

```python
ticks = db.table(
    "ticks",
    primary_key=["symbol", "ts"],
    time="ts",
    partition="1day",
)
```

未来文档主线优先展示 `db.table(...)`，`timeseries()` 放在“快捷别名”章节。

## 推荐分层

### 第一层：普通用户主 API

```python
table = db.table(
    "ticks",
    primary_key=["ts", "symbol"],
    time="ts",
    partition="1day",
)
table.write(rows)
table.query(start=..., end=...).to_list()
```

目标：最少概念，能完成写入、读取、分区。

### 第二层：高级契约 API

```python
spec = TableSpec(...)
table = db.table("ticks", spec=spec)
```

目标：可序列化、可复用、可测试、可做布局/重分区/session。

### 第三层：低层兼容/逃生 API

```python
table.insert_all(rows, pk=[...], partition=DurationPartition(...))
table.select("*", where={...})
```

目标：兼容旧代码、支持底层调试，不作为新用户文档主线。

### 第四层：mysqlite 工具层

`iosql.utils.mysqlite` 保留给单文件 SQLite、schema-on-write、连接池等工具场景。它不应混入主时序 Table 教程。

## 迁移建议

### 阶段 1：只加语法糖，不改存储

新增：

- `Database.table(name, primary_key=None, time=None, partition=None, columns=None, ..., spec=None)`
- `Table.define(primary_key=None, time=None, partition=None, columns=None, ...)`
- partition 输入规范化函数：支持字符串、`PartitionSpec`、`DurationPartition`、`CalendarPartition`、dict。

内部仍生成当前 `TableSpec`，不改 metadata 格式和底层分区实现。

### 阶段 2：更新文档和示例

文档结构改为：

1. 原始 `Table` 简洁版。
2. `timeseries()` 快捷别名。
3. `TableSpec` 高级版。
4. 低层 `insert_all/select` 兼容版。
5. `mysqlite` 单文件工具层。

### 阶段 3：兼容旧写法但加 warning

对新代码不推荐：

```python
table.partition = partition
table.insert_all(rows, pk=[...])
```

可以继续支持，但当表已有 `TableSpec` 且用户试图直接改 `table.partition` 时，应警告或拒绝，避免 metadata 和 runtime 状态分裂。

## 风险和边界

- 过多入口会造成文档混乱。需要明确主 API、快捷别名、高级 API、低层逃生四层。
- 自动推导 `time` 虽减少配置，但必须可解释、可覆盖，并在歧义时拒绝。
- 允许写入时 bootstrap 会增加 `write()` 参数复杂度，需要严格限制为“首次定义或兼容校验”。
- 旧 `select()` where AST 仍暴露内部表达式，不适合继续作为入门文档主线。
- `DurationPartition(interval_ms="1day")` 当前命名中 `interval_ms` 接收字符串，语义有点别扭；如果暴露为主文档，建议新增更自然的构造器，例如 `Partition.duration("1day", by="timestamp")`。

## 最终选择

如果现在可以重新设计，我会选择：

1. `Database.table(...)` 成为唯一主入口，直接接收 `primary_key/time/partition/columns`。
2. `TableSpec` 保留为内部持久化模型和高级显式配置，不要求入门用户手写。
3. `Database.timeseries()` 保留为快捷别名，但不作为文档唯一入口。
4. 吸收旧接口的优点：`db["table"]` 直接性、分区对象可传入、首次写入可 bootstrap、`select()` 低层逃生。
5. 不吸收旧接口的核心缺点：不让主键和分区长期散落在每次 `insert_all()` 调用里，不允许可变 `table.partition` 绕过表级契约。

也就是说，理想形态不是“旧接口 vs 当前接口二选一”，而是：

```text
旧接口的轻量表达 + 当前接口的表级契约和查询语义
```

主 API 应该让用户感觉自己在操作一张普通 Table，同时系统内部仍然用 `TableSpec` 维护稳定、可验证、可演进的时序契约。
