# user-api-unify-20260822-overview

## 背景

通读 `examples/`（12 个主线示例 + 4 个金融示例 + 3 个 mysqlite 示例）后，按 `api-design-zh` skill 的接口设计原则逐项审视，发现用户接口层存在**概念碎片化**：同一心智模型（"建一张分区时序表 + 写入 + 查询"）在示例里表现为多套并行 API、同名异义、运算符拼写不一致。存储层的正确性（契约、危险操作显式化、批量语义、裁剪）已做得很扎实，但用户接口的统一性和一致性还有优化空间。

本设计聚焦"用户接口"（面向业务开发者/Agent 调用者），不涉及内部存储层改造。

> 修订说明：初稿基于示例审阅提出了一些问题，经与 `iosql/database.py`、`iosql/table.py`、`iosql/engines/sqlite/table.py`、`iosql/core/query/*` 实际代码核对后，修正了若干不准确的表述。最终问题清单和结论以此修订版为准。

## 现状问题清单（已与源码核对）

### P0-1 · `timeseries()` 是一套独立的扁平参数词汇表

`table()` 与 `timeseries()` 的**真正不对称**不在于"同一概念不同词"，而在于 `timeseries()` 根本不是 `table()` 的薄封装——它拥有自己的一整套扁平参数：

```python
# table() 已同时接受 pk 与 primary_key，且相互冲突检查
def table(self, table_name, spec=None, pk=None, primary_key=None, columns=None,
          partition=None, segment=None, order_key=None, conflict='append',
          schema_policy='evolve', force_partition=False): ...

# timeseries() 只接受 primary_key（不接受 pk），且带一整排专属扁平参数
def timeseries(self, table_name, time, primary_key, columns=None,
               partition_every='1day', timezone='UTC', schema_policy='evolve',
               symbol_column='symbol', session_column=None,
               target_rows_per_file=400_000, min_rows_per_file=300_000,
               max_rows_per_file=500_000, time_unit='ms', partition_kind='duration'):
    spec = TableSpec.timeseries(time=..., primary_key=..., partition_every=...,
                                timezone=..., time_unit=..., partition_kind=...,
                                symbol_column=..., session_column=...,
                                target_rows_per_file=..., ...)
    return self.table(table_name, spec=spec)
```

`timeseries()` 通过 `TableSpec.timeseries(...)` 拼契约，再 `self.table(name, spec=spec)`。它的 `partition_every` / `timezone` / `time_unit` / `partition_kind` / `symbol_column` / `session_column` / `target|min|max_rows_per_file` 构成一套**与 `table()` + `Partition.*` 不重叠的词汇**。

后果：用户学完 `01_quickstart_table`（`Partition.duration("ts", every=...)`）再学 `01_quickstart_timeseries`（`partition_every="1day"`），同一"分区周期"概念要用两套词表达，等于重新学一套 API。

### P0-2 · `partition` 三种表达形式字段名不统一

同一 `partition` 概念有三种写法：

1. 对象：`Partition.duration("ts", every="1day")`
2. 字符串：`partition="duration(ts,every=1day)"`（例 07/08/09，走 `PartitionSpec` 解析）
3. helper 简写：`partition_every="1day"`（timeseries，走 `TableSpec.timeseries`）

三种形式本身可作为"渐进式复杂"分层（合理），但字符串/对象里的 `every` 与 helper 里的 `partition_every` 不是同一个词，用户在三种形式间转换时需要翻译。

### P1-1 · 运算符拼写不一致（功能已容错，仅规范写法问题）

- 例 01/03/05 用 `"op": "=="`
- finance/04 用 `"op": "="`

**已核对**：解析层 `iosql/core/query/case_util.py` 已同时接受 `['eq', '==', '=']`，`text_where.py` 的 `_COMPARISON_OPS` 同时含 `=` 与 `==`。即功能上两者**均已支持**，问题仅在示例/文档示范了两种拼写，而非缺少容错。

### P1-2 · 规范 `op` 输出用 `between_and`（内部命名）

`op="between_and"` 是解析器内部的规范形式（`case.py`、`text_where.py` 都把 `between` 归一化为 `between_and`）。`text_where.py` 已接受用户写 `between` 并映射为 `between_and`。**已核对**：容错已存在，问题在于示例示范了 `between_and` 这个内部命名，属于 `api-design-zh` 反模式"暴露内部实现"。

### P1-3 · 写入方法分层但命名不体现层级

已核对 `write()` 与 `insert_all()` 是**分层对**，不是"两个名字同一件事"：

- `write(rows, mode=None)`：高层、契约感知，内部解析 `spec.primary_key` / `spec.to_partition()` / `spec.conflict`，把 `mode` 映射为 `replace/ignore` 布尔后**委托给 `insert_all()`**（`engines/sqlite/table.py:487`）。
- `insert_all(data, pk=..., replace=False, ignore=False, ...)`：低层、需显式传 pk/partition/冲突标志。

问题：示例里 `01/02/05/06` 用 `insert_all`（低层），`04/07/09/10/11/12` 及全部 finance 用 `write`（高层），两者都出现在"主 API"示例里，用户看不出层级关系，容易混用。

### P1-4 · 查询返回语义不一致

已核对：
- `select(...)` → 在 `engines/sqlite/table.py:2537` **急切构建并返回完整列表**。
- `query(...)` → 返回 fluent `_TableQuery`（`.to_list()`、`.explain()`）。

同一库两种返回约定，示例 `01_quickstart_table` 用 `select(...)` 直接拿行，`01_quickstart_timeseries` 用 `query(...).to_list()`。

### P2-1 · 冲突控制命名不统一（机制已连通）

已核对：`write(mode=None)` **默认回落到 `spec.conflict`**（`table.py:471-472`），`mode` 取值 `['append','upsert','replace','ignore']`，再映射为 `insert_all` 的 `replace/ignore` 布尔。即调用侧 `mode` 与契约侧 `conflict` **本就连通**，不是两套机制。

真正问题：
- 同一概念在调用侧叫 `mode`、在契约侧叫 `conflict`，命名不统一。
- 高层用 `mode`（字符串枚举），低层 `insert_all` 用 `replace/ignore`（布尔），两层表达同一语义的方式不同。

### P2-2 · `time_unit` / `partition_kind` 内部味命名

`timeseries()` 的 `time_unit="date"` + `partition_kind="calendar"` 偏内部实现命名；`partition_kind="calendar"` 与 `table()` 里的 `Partition.calendar(...)` 是同一概念的另一套词（实为 `TableSpec.timeseries` 内部的 `PartitionSpec` 拼装）。

## 目标

1. 全库收敛为**一个概念模型**：`Table` + `Partition` + 统一写入/查询动词。
2. `timeseries()` 收敛为 `table()` 的**薄语法糖**：只做默认 `partition=duration`、隐含 `order_key=time` 的封装，参数名与 `table()` 对齐；仅保留 `query/latest/asof` 这些真正的时序业务便捷方法（`symbol_column`/`session_column` 等保留为显式可选参数）。
3. `partition` 的对象、字符串、helper 三种形式共享**同一个关键词汇表**（统一 `every`）。
4. 统一运算符**规范写法**（功能容错已具备，只需统一示例/文档示范）。
5. 明确写入方法层级，统一查询返回语义，统一冲突控制命名。

## 非目标

1. 不改变存储层目录结构、分区 key 计算、segment 物理布局。
2. 不引入新的分区类型（`hash`、`value` 等）。
3. 不做内部接口（`iosql.utils.mysqlite`）的命名改造，它已有自己的文档边界。
4. 不在本轮实现跨 segment 的全局 upsert/tombstone。
5. 不强制一次性重命名所有方法；采用兼容别名 + 文档推荐的方式渐进迁移。

## 接口设计

### 一、统一主键参数名

`table()` 已同时接受 `pk` 与 `primary_key`。把 `timeseries()` 补上 `pk` 别名，使其与 `table()` 一致；统一推荐 `pk`：

```python
db.table("ticks", pk=["symbol", "ts"], ...)            # 已支持
db.timeseries("ticks", pk=["symbol", "ts"], time="ts", ...)  # 新增 pk 别名
db.timeseries("ticks", primary_key=["symbol", "ts"], ...)    # 兼容保留
```

### 二、`timeseries()` 收敛为 `table()` 语法糖（核心改动）

`timeseries()` 不再走独立的 `TableSpec.timeseries(...)` 词汇表，改为**薄封装 `table()`**，仅注入 `partition=Partition.duration(time, every=...)` 与 `order_key=time`，其余参数透传：

```python
def timeseries(self, table_name, time, pk, columns=None, *,
               partition_every="1day", timezone="UTC", symbol_column="symbol",
               session_column=None, **table_kwargs):
    partition = Partition.duration(time, every=partition_every, timezone=timezone)
    return self.table(
        table_name, pk=pk, columns=columns, partition=partition,
        order_key=time, symbol_column=symbol_column,
        session_column=session_column, **table_kwargs,
    )
```

- 保留 `time`（决定分区列与 `order_key`）这个时序专属便捷参数。
- `partition_every` / `timezone` 保留为便捷简写（因为它们本质就是"用 duration 分区"的默认值），但**与 `Partition.*` 关键词汇表对齐**（用 `every`/`timezone`）。
- `partition_kind`、`time_unit` 作为**已废弃**入参（改为建议用 `Partition.calendar(...)` / `Partition.range(...)` 传入 `partition=`），新文档不再示范。
- `symbol_column` / `session_column` 保留为显式可选参数（它们是时序业务概念，不是分区实现细节）。
- `target/min/max_rows_per_file` 与 `Segment.rows(...)` 对齐的语义，作为 `segment=` 透传；在 `table()` 上若已能表达则不必在 `timeseries()` 重复暴露。

> 权衡：若把 `timeseries()` 完全变成"只接受 `partition=` 对象"，会丢失 `partition_every/timezone` 的简写便利。本设计保留这两个简写，但让它们**复用 `Partition.*` 的词汇**，从而消除"同一概念两套词"。

### 三、`partition` 关键词汇表统一

对象、字符串、helper 三种形式共用 `every` / `timezone`：

```python
Partition.duration("ts", every="1day")                        # 对象
"duration(ts,every=1day)"                                     # 字符串
db.timeseries("ticks", time="ts", pk=[...], partition_every="1day")  # helper 简写，复用 every 语义
```

helper 的 `partition_every` 语义与字符串/对象里的 `every` 对齐（内部最终都产出 `Partition.duration(ts, every=...)`）。

### 四、统一运算符规范写法

解析层**已容错** `==`/`=` 与 `between`/`between_and`，无需新增别名。本项只需：

- 示例与文档统一示范 `op="between"`、`op="=="`（用户语言）。
- `between_and` 作为**内部规范形式**保留（解析器归一化目标），但不在用户示例中示范。

### 五、明确写入方法层级

不删除 `insert_all`（它是低层基础），但明确**主 API 推荐 `write`**，示例统一示范 `write`：

```python
table.write([...])                  # 高层：契约感知，推荐主用
table.insert_all([...], pk=..., replace=False, ignore=False)  # 低层：显式传参，进阶
with table.write_buffer(max_rows=10000) as buffer:
    buffer.append(rows)             # 缓冲累积（语义不同，保留）
```

### 六、统一查询返回语义

`select(...)` 与 `query(...)` 返回约定不一致（列表 vs fluent 对象）。推荐方案（见开放问题 1）：

- 保持 `select(...)` 直接返回列表（最直观）。
- `query(...)` 保留 fluent（`.to_list()`/`.explain()`），但文档显著标注差异。
- 新示例统一使用一种风格，避免示范两套。

### 七、统一冲突控制命名

机制已连通（`write(mode=...)` 回落到 `spec.conflict`）。本项只做命名统一：

- 调用侧统一推荐 `mode`（字符串枚举 `append/upsert/replace/ignore`），文档说明其默认来自契约 `conflict`。
- 或把 `write` 参数改名 `conflict` 对齐契约侧。**倾向保留 `mode`**（`mode` 是写入动词的自然搭配），在文档明确 `mode` 与契约 `conflict` 的关系。

## CLI 影响

`--partition` 字符串语法保持不变（`duration(ts,every=1hour)`），与统一词汇表一致。无需变更。

## 测试设计

1. `timeseries(partition_every=...)` 与 `table(partition=Partition.duration(time, every=...))` 创建的契约等价（`spec()` 一致）。
2. `timeseries(pk=...)` 与 `timeseries(primary_key=...)` 等价。
3. `op="between"` 与 `op="between_and"`、`op="=="` 与 `op="="` 查询结果一致（回归验证已有容错）。
4. `write(mode=...)` 与契约 `conflict` 回落的组合行为正确。
5. 废弃参数（`partition_kind`、`time_unit`）产生等价结果并触发弃用日志。
6. `write()` 与 `insert_all()` 对同一批数据结果一致（`write` 委托 `insert_all`）。

## 开放问题

1. **查询返回语义**：`query()` 是否改为默认急切返回行列表（与 `select()` 一致），而把 `.explain()` 拆成独立方法？还是保持 fluent 对象并在文档标注？倾向：**保持 fluent**（`query/latest/asof` 的 `.explain()`/`.to_list()` 是时序便捷查询的合理用法），文档显著标注与 `select()` 的差异，新示例统一示范一种风格。
2. **`timeseries()` 简写去留**：是否完全移除 `partition_every/partition_kind/time_unit`，强制用户用 `partition=Partition.*(...)` 对象或字符串？倾向：**保留 `partition_every`/`timezone` 简写**（与 `Partition.*` 词汇对齐），移除 `partition_kind`/`time_unit`（引导用 `Partition.calendar/range`）。

## 变更记录

- 2026-08-22 初稿：基于 examples/ 全量审阅的接口一致性设计。
- 2026-08-22 修订：与源码核对后修正 P0-1/P0-2/P1-1/P1-2/P1-3/P2-1 的事实表述（`table()` 已接受双主键名、`timeseries()` 是独立扁平词汇、运算符容错已存在、`write` 委托 `insert_all`、`mode` 回落 `conflict`），并据此收敛接口设计建议。
