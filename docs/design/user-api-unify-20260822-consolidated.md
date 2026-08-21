# user-api-unify-20260822-consolidated

## 定位

本文合并并修正前两份接口评审：

- 前作 A：`examples/` 全量审阅（初评，聚焦示例暴露的人机工学问题）。
- 前作 B：`docs/design/user-api-unify-20260822-overview.md`（与源码核对的接口统一设计）。

本文对两份文档的**全部关键事实声明逐条重新核实**（源码阅读 + 运行时探针），保留经得起验证的结论，修正错误表述，并补充双方各自遗漏的问题。最终问题清单、接口设计建议以本文为准。

核实方法：直接阅读 `iosql/database.py`、`iosql/engines/sqlite/table.py`、`iosql/spec.py`、`iosql/partition/*.py`、`iosql/core/query/*.py`，并用 Python 探针实际验证 `Where` 对 dict/文本两种条件形式的解析行为。

## 一、事实核对总表

| # | 声明 | 出处 | 核实结果 |
|---|------|------|---------|
| 1 | `table()` 同时接受 `pk` 与 `primary_key` 且做冲突检查 | 前作 B | ✅ `database.py:122-170` |
| 2 | `timeseries()` 只接受 `primary_key`，且拥有独立扁平参数词汇表 | 前作 B | ✅ `database.py:172-195` |
| 3 | dict 形式 `op` 中 `==` 与 `=` 均已容错 | 前作 B | ✅ `case_util.py:28` 接受 `['eq','==','=']` |
| 4 | **dict 形式 `op="between"` 已被容错** | 前作 B（测试设计第 3 条） | ❌ **实测失败**。`between` 归一化只存在于文本表达式路径（`text_where.py:162-166`）；dict 形式 `{"op":"between","value":[1,10]}` 抛裸 `AssertionError`（`case.py:86`），只有 `between_and` 可用 |
| 5 | "对象、字符串、helper 三种 partition 形式共用 `every`/`timezone`" | 前作 B（设计第三节） | ❌ **未统一**。对象工厂 `Partition.calendar(timezone=)`（`base.py:27`）；字符串示例/README/doctor 输出全部示范 `tz=`（解析器 `spec.py:208-209` 将 `tz` 归一化为 `timezone`，两者均可解析）；内部类 `CalendarPartition.__init__(tz=)`（`calendar.py:71`）。三种拼写并存，只是解析层容错 |
| 6 | `write(mode=...)` 默认回落 `spec.conflict`，`write` 内部委托 `insert_all` | 前作 B | ✅ `engines/sqlite/table.py` `write()`/`insert_all()` |
| 7 | `select()` 急切构建并返回完整列表 | 前作 B | ✅ `table.py:2548` 起 `full_data` 聚合 |
| 8 | `query(start/end/date)` 需要 `TableSpec.time`，缺失时抛 `MissingTimeColumn` | 初评 | ✅ `table.py:1602-1605`；且 `TableContract.__post_init__` 只做 time→partition 单向增强（`spec.py`），**不从 partition 反推 time** |
| 9 | `select(fields, where)` 为必传位置参数，逼出示例写 `select("*", where=None, ...)` | 初评 | ✅ `table.py:2548`；`examples/10_existing_database_repartition.py:78` 实证 |
| 10 | 薄封装代码 `Partition.duration(time, every=..., timezone=...)` 可行 | 前作 B（设计第二节代码） | ❌ **跑不通**。`Partition.duration(column, every)` 无 timezone 参数（`base.py:17-20`，duration 分区按 UTC 对齐，无时区概念）；且 `Database.table()` 签名无 `symbol_column`/`session_column`（`database.py:122-135`），透传会 `TypeError` |
| 11 | `latest(by=..., group_by=...)` 存在等价别名 | 初评 | ✅ `table.py:610-618` |
| 12 | `select_all` 只是 `select(to_dict=True)` 的马甲 | 初评 | ✅ `table.py:2454-2455` |
| 13 | `WriteResult.rows_seen` = 输入行数 | 初评 | ✅ `insert_all` 中 `rows_seen=len(rows)` |

> 核对结论：前作 B 的"方向性判断"基本正确（timeseries 词汇独立、写入是分层对、容错已存在），但**三处细节失实**（#4、#5、#10），其中 #10 直接影响其设计代码的可实施性。初评的 query() 覆盖缺口（#8）与 select 签名问题（#9）为前作 B 完全遗漏，且均经源码证实。

## 二、最终问题清单

### P0-1 · `timeseries()` 是一套独立的扁平参数词汇表（前作 B，已核实）

`timeseries()` 不是 `table()` 的薄封装：它只接受 `primary_key`（不接受 `pk`），并通过 `partition_every` / `time_unit` / `partition_kind` / `target|min|max_rows_per_file` 等专属参数拼装 `TableContract.timeseries(...)`。用户学完 `01_quickstart_table`（`Partition.duration("ts", every=...)`）再学 `01_quickstart_timeseries`（`partition_every="1day"`），同一概念要换一套词。

### P0-2 · `query()` 便捷参数不覆盖普通分区表（初评，已核实）

金融时序 80% 的查询是 symbol + 时间窗口，`query(symbols/start/end/date/session)` 就是答案，但它要求契约里有 `TableSpec.time`。而 `db.table(..., partition=Partition.calendar("event_date", ...))` 快捷路径**不设置 time**，契约也不从 partition 反推——于是 example `02_calendar_events_table.py` 被迫退回 dict where 全手写。**partition 明明知道列名和语义，查询层却用不上**，用户要重复声明知识。

### P0-3 · `select(fields, where)` 必传位置参数（初评，已核实）

全表扫描是最简单的场景，却要写 `select("*", where=None, to_dict=True)`（example 10 实证）。默认应为 `fields="*", where=None`。

### P0-4 · 写入分层存在但层级不可见（合并修正）

`write(rows, mode=)`（高层、契约感知）与 `insert_all(data, pk=, replace=, ignore=)`（低层、布尔标志）是委托关系，不是重复 API——前作 B 的修正成立。但示例里 01/02/05/06 用低层、07~12 与 finance 全用高层，用户无从判断该用哪个；且低层的 `replace`/`ignore` 布尔与高层的 `mode` 枚举是同一语义的两种表达，同时传 `replace=True, ignore=True` 的行为不自明。

### P1-1 · 查询结果消费有三种模式（合并）

- `select(..., to_dict=True)`：标志位变形参，且 `to_dict` 命名有歧义（实际语义是"行转 dict 而非 tuple"）
- `query(...).to_list()` / `latest(...).one()`：惰性对象方法
- `select_all(...)`：只是 `select(to_dict=True)` 的马甲（`table.py:2454-2455`），flag 提升出来的冗余入口

### P1-2 · partition 时区词汇三种拼写并存（修正前作 B）

对象 `timezone=`、字符串文档示范 `tz=`、内部类 `tz=`。解析器已做 `tz→timezone` 归一化（`spec.py:208-209`），功能无碍，但 README、doctor 输出、示例全部示范 `tz=`，与对象 API 的 `timezone=` 不一致。`every` 已统一，时区没有。

### P1-3 · dict 条件的 op 词表：容错不完整 + 错误质量差 + 存在静默失效路径（升级）

- `==`/`=`：已容错（`case_util.py:28`），仅示例拼写不统一——前作 B 正确。
- `between`：**dict 形式不可用**，只有文本表达式路径归一化（实测探针证实）。前作 B 测试设计第 3 条"op='between' 与 op='between_and' 查询结果一致"对 dict 形式不成立。
- 错误质量：dict 形式传错 op 得到的是裸 `AssertionError`（`case.py:86`），不是带修复指引的 `BadArgument`——违反"用户可恢复的错误应给出明确的修复指引"。
- **静默失效链（2026-08-22 探针补充，比"错误质量差"更严重）**：`Case('symbol', 'bad_op', 'x')` 构造时**静默通过**（op 字符串不校验，`check_field_tvalue` 只 assert 值类型）→ `get_case_range`（`case_util.py:28-31`）对未知 op 返回 `(op, None, None)` → **分区/segment 裁剪静默失效**（性能退化无任何提示）→ 直到 SQL 编译层 `query_util.py:96` 才抛 `BadArgument('Unsupported op')`（无合法 op 列表、无修复示例）。拼错 op 的代价不是立即报错，而是"查询悄悄变慢 + 延迟报错"。op 归一化白名单应放在 `Case.__init__` 入口，一处改动同时修掉三个问题。

### P1-4 · `latest(by=)` 与 `group_by=` 别名（初评，已核实）

别名不会降低学习成本，只会让文档和报错出现两个词。应删除 `group_by`。

### P1-5 · segment 阈值参数两套命名（初评）

`Segment.rows(max_rows/target_rows/min_rows)` vs `timeseries(target_rows_per_file/min_rows_per_file/max_rows_per_file)`。同一概念在两层 API 用不同词。P0-1 的薄封装改造若让 `timeseries` 通过 `segment=` 透传，此问题随之消解。

### P1-6 · evolve（动态扩列）语义契约未定义（2026-08-22 补充）

`schema_policy='evolve'` 是默认行为（`database.py:133`、`spec.py:637`），写入时动态加列（`ALTER TABLE ADD COLUMN`，O(1)）已可用，但三个语义缺口未定义，宽表场景（300+ 列持续增长）会放大：

1. **跨分区 schema 漂移**：每个分区是独立 SQLite 文件。第 N 天出现的新列只存在于第 N 天及之后的分区文件；查询 union 各分区时，旧分区该列的行为（NULL 补齐？报错？）无契约定义。
2. **契约回写**：动态加列后，持久化的 `TableSpec.columns` 是否同步更新？若不更新，重开库（例 09 零仪式场景）后 `spec()` 与实际 schema 漂移，`select` 新列可能在裁剪/编译层被拒。
3. **类型冲突**：同名列先 int 后 str 的演进策略未定义（窄化/报错/声明优先）。

### P1-7 · 导出面过宽 + `Case` 魔法运算符（2026-08-22 补充）

- `iosql/__init__.py` 导出 26 个名字；`TableContract` 与 `TableSpec` 近义概念并列导出，用户无法区分。例 08 被迫手写 `TableContract(TimeColumn(...), PhysicalLayout(...))` 构造——其中绝大部分可由 `db.table()` 快捷参数表达（依赖设计一的 `table()` 参数补齐）。
- `Case` 的 `*`=and / `+`=or 运算符重载（`case.py:88-98`）在全部示例中零使用，属于"多余魔法暴露"；`CaseExpression` 是其配套实现细节。用户层文档不应示范，导出可保留（兼容）但标注内部。

### P1-8 · `with_partition` 双签名（2026-08-22 补充）

例 04 用 `with_partition(every="1hour")`，README 用 `with_partition(partition="duration(ts,every=1hour)")`——同一方法两种形态并存于示范层。应统一示范 `partition=` 完整表达；`every=` 保留为兼容快捷。

### P2-1 · spec 缺失的错误路径把检查推给用户（初评）

example 09/10 手写 `if spec is None: raise RuntimeError(...)` 防御代码。`write()` 内部已有 `BadArgument('write requires a TableContract')`，但不含修复指引（应说明"该表存在但无持久化契约，请传入建表参数或用 spec= 绑定"）。示例应示范信任 API 的错误路径。

### P2-2 · `pk` / `primary_key` 双拼写（合并）

`table()` 已双收并做冲突检查；`timeseries()` 只收 `primary_key`。方向：统一推荐 `pk`，`timeseries` 补 `pk` 别名，长期 deprecated `primary_key`。

### P2-3 · 示例工程卫生（初评）

- quickstart（01）裸调 `db.close()` 无 try/finally，异常路径泄漏连接；09~12 已用 `with Database(...)`。入门示例应教上下文管理器。
- `rows_seen` 命名含糊（实为输入行数），`rows_received` 更直白。
- 流式入口有 `iter_select` / `iter_select_batches` / `query().iter_batches` / `query().iter_rows` 四个，应收敛为 batch 为主、row 为语法糖。

## 三、接口设计建议

### 一、`timeseries()` 收敛为 `table()` 薄语法糖（方向采纳前作 B，代码修正）

前作 B 的方向正确，但其代码草图有两处不可实施，修正后的可行方案：

**前置条件**：`Database.table()` 需要新增 `time=None`、`symbol_column=None`、`session_column=None` 三个参数（`TableContract` 已有这三个字段，缺口只在 `table()` 参数面），使契约感知能力下沉到主入口。

```python
def table(self, table_name, spec=None, pk=None, columns=None,
          partition=None, segment=None, order_key=None, conflict='append',
          schema_policy='evolve', force_partition=False,
          time=None, symbol_column=None, session_column=None):  # 新增
    ...

def timeseries(self, table_name, time, pk=None, primary_key=None,
               columns=None, *, partition_every="1day", timezone="UTC",
               symbol_column="symbol", session_column=None, **table_kwargs):
    # duration 分区按 UTC 对齐、无时区概念；timezone 仅对 calendar 有意义
    if table_kwargs.pop("partition_kind", None) == "calendar":
        partition = Partition.calendar(time, every=partition_every, timezone=timezone)
    else:
        partition = Partition.duration(time, every=partition_every)
    return self.table(
        table_name, pk=pk, primary_key=primary_key, columns=columns,
        partition=partition, order_key=time,
        time=time, symbol_column=symbol_column, session_column=session_column,
        **table_kwargs,
    )
```

要点：

1. **`Partition.duration` 不接受 timezone**——duration 分区边界按 UTC 对齐是文档化行为，薄封装不得虚构该参数。`timezone` 只在 calendar 分支透传。
2. `partition_kind` / `time_unit` 废弃：`partition_kind="calendar"` 改为直接传 `partition=Partition.calendar(...)`；`time_unit="date"` 的场景本就应使用 calendar 分区（日期字符串）。
3. `target/min/max_rows_per_file` 不再在 `timeseries()` 暴露，统一走 `segment=Segment.rows(...)`——顺带消解 P1-5 的双命名。
4. `pk` 别名补齐，`primary_key` 兼容保留。

### 二、`query()` 便捷参数下放到普通表（初评补充，前作 B 未覆盖）

契约感知的 `query(symbols/start/end/date/session)` 不应绑定 `timeseries()`。两条路径任选：

- **路径 A（推荐）**：`TableContract.__post_init__` 在 `time is None` 且 partition 可提供语义时，从 partition 反推 time（calendar/duration 分区都持有 `ts_col`）。信息已在契约里，不应要求用户重复声明。
- **路径 B**：`table()` 新增 `time=` 参数（见设计一），用户显式声明一次，`query()/latest()/asof()` 随契约可用。

路径 A 对存量用户零成本；路径 B 表达更显式。可两者都做：`time=` 显式优先，partition 反推兜底。

### 三、`select` 签名默认值（初评补充）

```python
def select(self, fields="*", where=None, limit=None, order_by=None, to_dict=False, **kwargs):
```

消除 `select("*", where=None, ...)` 样板。兼容性无风险（纯放宽）。

### 四、dict 条件 op 词表补全 + 错误质量（修正前作 B 测试设计）

1. `Case.__init__` 增加 op 归一化：`"between"` → `"between_and"`（与文本表达式路径对齐，一处改动）。
2. `check_field_tvalue` 的 `AssertionError` 替换为 `BadArgument`，错误信息给出合法 op 列表与修复示例。
3. 示例与文档统一示范 `op="=="`、`op="between"`（用户语言）；`between_and` 作为内部规范形式不再出现在示例中。

### 五、partition 时区词汇统一（修正前作 B 第三节）

解析层 `tz`/`timezone` 均可解析（已归一化），统一动作只需改**示范层**：

- README、doctor 输出、示例中的字符串 partition 统一示范 `timezone=`（与对象 API 一致）。
- `tz=` 保留为文档标注的官方别名，不删除（存量 CLI 脚本兼容）。

### 六、写入与查询的示范收敛（采纳前作 B）

- 示例统一示范 `write(rows, mode=...)` 为高层主入口；`insert_all` 在文档中明确标注为低层基础（进阶场景），并补充 `replace`/`ignore` 互斥校验。
- `select()` 保持急切列表、`query()` 保持 fluent 对象，文档显著标注差异，新示例统一风格。
- `select_all` 标记 deprecated（它是 `select(to_dict=True)` 的马甲）。
- `latest(by=)` 保留，`group_by` 别名 deprecated 后删除。

### 七、错误指引与示例卫生（初评补充）

- `write()` 无契约时的错误信息补充修复指引；example 09/10 删除手动防御代码。
- quickstart 示例改用 `with Database(...)` 上下文管理器。
- `WriteResult.rows_seen` → `rows_received`（兼容别名过渡）。
- 流式入口文档收敛：`iter_batches` 为主形态，`iter_rows` 说明为其语法糖。

### 八、evolve 语义契约（2026-08-22 新增，P1-6 的解）

三个缺口的建议语义（实施时以源码可达性微调，语义本身不可再开放）：

1. **列集合单调增长**：evolve 只允许加列，不允许删列/改列名。查询 union 各分区时，旧分区缺失的列以 `NULL` 投影补齐（与 SQL `union all + null` 语义一致），并在 `explain_select` 中不视为异常。
2. **契约回写**：动态加列成功后，同步更新持久化 `TableSpec.columns`（含推断类型）。保证 `spec()` 永远等于"当前实际 schema 的上确界"，例 09 式零仪式重开不漂移。
3. **类型冲突**：契约已声明的列按声明优先（数据不匹配时写入报错，即现有行为）；未声明列首次推断类型后固化进契约，同列后续数据类型不兼容时抛 `BadArgument`（错误信息含列名、已固化类型、本次值类型与修复建议——重新声明契约或转换数据）。

### 九、导出面分层与示范收敛（2026-08-22 新增，P1-7/P1-8 的解）

- `__init__` 导出保持兼容不变；README 将导出分两层示范——**用户层**：`Database` / `Partition` / `Segment` / `Where` + 结果类型（`WriteResult`/`QueryPlan`/`LayoutReport` 等）；**高级层**（迁移与诊断）：`TableContract`/`TableSpec`/`PhysicalLayout`/`TimeColumn`，并注明"业务建表优先 `db.table(...)` 快捷参数"。
- `Case`/`CaseExpression` 运算符重载不出现在任何用户文档与示例；dict/文本 where 是唯一示范形式。
- `with_partition` 文档与示例统一示范 `partition=` 完整表达；`every=` 标注为兼容快捷。
- `select`/`iter_select` 增加 `columns=` 别名（与 `query(columns=)` 对齐），`fields=` 兼容保留。

## 四、与前两份文档的差异说明

1. **修正前作 B**：dict 形式 `op="between"` 实测不可用（其测试设计第 3 条失效）；"三种 partition 形式共用 timezone"与事实不符（字符串示范层全用 `tz=`）；薄封装代码草图两处不可实施（`Partition.duration` 无 timezone 参数、`table()` 无 `symbol_column/session_column` 参数），已给出修正版方案。
2. **补充初评独有问题**（前作 B 未覆盖，均已源码证实）：query() 便捷参数不覆盖普通分区表（P0-2）、select 必传位置参数（P0-3）、结果消费三模式与 `to_dict` 命名（P1-1）、`group_by` 别名（P1-4）、segment 阈值双命名（P1-5）、spec 缺失错误指引（P2-1）、示例工程卫生（P2-3）。
3. **采纳前作 B 的修正**：`==`/`=` 容错已存在（降级为示范层问题）；`write`/`insert_all` 是分层对而非重复 API；`timeseries()` 独立词汇表这一 P0 级发现。

## 五、测试设计

1. `timeseries(pk=...)` 与 `timeseries(primary_key=...)` 创建的契约等价。
2. `timeseries(...)` 与等价 `table(..., partition=Partition.duration(...), order_key=..., time=...)` 的 `spec()` 一致。
3. **dict 形式** `op="between"` 与 `op="between_and"` 查询结果一致（需先实现第四节第 1 条归一化；当前实测不一致，此为新增能力验证而非回归）。
4. dict 形式非法 op 返回 `BadArgument`（含修复指引与合法 op 列表），不再泄漏 `AssertionError`；**且在 `Case` 构造时即抛出**（堵静默失效链），`get_case_range` 不再可能收到未知 op。
5. 字符串 partition `tz=` 与 `timezone=` 解析结果一致（回归验证已有容错）。
6. `write(mode=...)` 与契约 `conflict` 回落组合行为正确；`replace=True, ignore=True` 同时传入时抛 `BadArgument`。
7. calendar 分区表（`db.table(partition=Partition.calendar(...))`，无显式 time）执行 `query(date=...)` 可用（验证第二节路径 A 的 partition 反推）；`range` 分区不反推（开放问题 1 的既定不对称）。
8. `write()` 与 `insert_all()` 对同一批数据结果一致（`write` 委托 `insert_all`）。
9. evolve 三语义（2026-08-22 新增）：加列后旧分区查询该列返回 NULL；加列后 `spec().columns` 包含新列且重开库后仍在；未声明列类型冲突抛 `BadArgument` 且信息含列名与两种类型。
10. `select()`/`iter_select()` 的 `columns=` 别名与 `fields=` 等价（2026-08-22 新增）。
11. `partition_kind`/`time_unit`/`*_rows_per_file`/`group_by`/`select_all` 触发 `DeprecationWarning` 且行为等价（2026-08-22 新增）。

## 六、开放问题

1. **partition 反推 time 的边界**：`range(id, width=...)` 分区的列不一定是时间语义，反推应只对 duration/calendar 生效，range 保持不反推——是否接受这一不对称？
2. **`to_dict` 标志的长期形态**：保留标志位（兼容）还是迁移到统一的结果对象（`.to_list()`）？倾向：新代码示范结果对象风格，标志位兼容保留。
3. **`rows_seen` 更名**：是否值得为命名清晰度付出一次兼容别名成本？倾向：v0.3 统一处理。
4. **`open_table()` 显式入口**（2026-08-22 补充）：对 P2-1，轻方案是"改错误信息 + 删示例防御代码"；重方案是拆 `db.open_table(name)`（无契约即抛错）/ `db.table(name, ...)`（纯声明）。重方案语义更显式但增加一个入口概念。倾向：本轮先做轻方案，重方案列入 P1（doctor/autotune 阶段）再评估。
5. **`Case` 运算符重载是否移除**（2026-08-22 补充）：零使用但属破坏性变更。倾向：保留实现、文档不示范，v0.4 评估移除。

## 七、范围决策记录（2026-08-22 拍板）

1. **本轮只做 SQLite 后端的接口稳定**。宽表列存后端（Parquet+DuckDB）经基准评估后推迟：垂直列组拆分仅在"查询域内聚"时有 2.6~12x 收益、跨组 JOIN 反超；每列一表方案写入慢 26x、空间 2x、读 60 列慢 3.3x。两者均不胜任任意列组合的量化分析负载，原生列存是远期终态而非当前目标。后续路线见 `auto-partition-20260822-overview.md`（双模式定位、P1.5 引擎抽象层）。
2. **接口词汇保持后端中立**：本设计全部新增参数（`time`/`symbol_column`/`session_column`/`segment`/`columns` 别名/`mode`）不绑定 SQLite 特有概念，为将来 `engines/parquet` 复用同一 `db.table()` 接口面留出空间。
3. **不包含 auto-partition（P1+）**：双模式（显式分区主力 + auto 可选层）按 `auto-partition-20260822-overview.md` 独立推进，本轮不实现。

## 八、期望的用户接口（目标形态与示例场景覆盖）

本节是第三节设计建议的"成品预览"，也是 PLAN-052 的验收基准：以下每个片段即一条可执行的验收用例，且必须覆盖 `examples/` 现有全部使用场景（对照表见末尾）。★ 标记 = 相对现状的新行为。

### 1. 快速上手：建表 + 写入 + 查询

```python
from iosql import Database, Partition

with Database("sqlite://database/market.iosql") as db:                      # ★ 入门示范一律 with
    ticks = db.table(
        "ticks",
        pk=["symbol", "ts"],                                               # 唯一推荐拼写
        columns={"symbol": "text", "ts": "integer", "price": "float"},
        partition=Partition.duration("ts", every="1day"),                  # ≡ "duration(ts,every=1day)"
    )
    ticks.write([...])                                                     # 唯一推荐写入动词

    rows = ticks.select(                                                   # ★ 参数全可省: select() = 全表
        where="symbol == 'AAPL' and ts between 1735689600000 and 1735776000000",
        order_by="ts",
    )                                                                      # 返回 dict 行列表 (默认 to_dict)
    plan = ticks.explain_select(where=...)                                 # 同一套参数
```

### 2. 业务时间查询：普通分区表直接可用（不再要求 timeseries）

```python
window = ticks.query(symbols="AAPL", start=..., end=...)                   # ★ partition 反推 time 契约
rows = window.to_list(); window.explain()

snap = ticks.latest(symbols=["AAPL", "MSFT"], as_of=..., by=["symbol"]).to_list()   # by= 唯一拼写

# 交易日/session（期货夜盘）: 建表时声明一次即可
futs = db.table("futures", pk=["symbol","ts"], columns={...},
                partition=Partition.duration("ts", every="1day"),
                session_column="trading_date")                             # ★ table() 直达，无需 timeseries
futs.query(session="2026-01-06", symbols="AU")
```

### 3. 时序语法糖：`timeseries()` = table() 的薄封装

```python
ticks = db.timeseries("ticks", time="ts", pk=["symbol", "ts"],             # ★ 补 pk 别名
                      columns={...}, partition_every="1day")
# 完全等价于: db.table(..., partition=Partition.duration("ts", every="1day"),
#                      order_key="ts", time="ts")
# ★ partition_kind/time_unit/*_rows_per_file → DeprecationWarning + 等价迁移提示
#   calendar 场景: db.timeseries(..., partition=Partition.calendar(...)) 或 db.table(...)
```

### 4. 日期字符串/日历表、缓冲写入、大宽表 evolve

```python
events = db.table("events", pk=["event_date", "name"],
                  columns={"event_date": "text", "name": "text", "city": "text"},
                  partition=Partition.calendar("event_date", every="1day", timezone="Asia/Shanghai"))
events.write([{"event_date": "2026-01-06", "name": "review", "city": "Beijing", ...}])
events.query(date="2026-01-06")                                            # ★ calendar 表也反推 time

book = db.table(..., segment=Segment.rows(max_rows=1_000_000), order_key="ts")
with book.write_buffer(max_rows=100_000) as buf:                           # 不变
    buf.append(batch)

# ★ evolve 三语义: 新列自动加、旧分区读为 NULL、契约回写、类型冲突带指引报错
factors = db.table("factors", pk=["symbol","ts"], columns={"symbol":"text","ts":"integer"}, ...)
factors.write([{"symbol":"AAPL","ts":...,"f1":0.3,"f2":0.7}])              # f1/f2 自动加列并固化进 spec()
factors.select(columns=["symbol","ts","f1"])                               # ★ columns= 别名
```

### 5. 重开已有库：零仪式

```python
with Database(uri) as db:
    ticks = db.table("ticks")                                              # 持久化契约直接可用，无防御代码
    ticks.write(new_rows, mode="ignore")

# 无契约表的错误体验:
# BadArgument: 表 'ticks' 没有持久化契约，无法 write()。
#   修复: db.table("ticks", pk=..., columns=..., partition=...) 重新声明，
#         或 db.table("ticks", spec=...) 绑定已有契约。                     # ★ 错误即指引
```

### 6. 检查与重分区（模式 A 闭环，保持现状安全阶梯）

```python
report = ticks.layout_report()                                             # 只诊断
new_spec = ticks.spec().with_partition(partition="duration(ts,every=1hour)")   # ★ 单一签名示范
ticks.repartition_to("sqlite://database/hourly.iosql", spec=new_spec)     # 写新库，源库不动
ticks.compact(); ticks.validate(deep=True); ticks.cleanup_obsolete()      # dry-run 默认，不变
# 拼错 op 即时报错: BadArgument: 不支持的 op 'betweeen'。合法 op: ==, !=, >, >=, <, <=, between, in
#   示例: {"field": "ts", "op": "between", "value": [1, 10]}              # ★ 堵静默失效链
```

### 7. 迁移（不变，仅示范收敛）

```python
source.to_postgresql(dsn, tables={"ticks": "pg_ticks"}, dry_run=True)     # 先预览
source.to_postgresql(dsn, tables={"ticks": "pg_ticks"}, verify="row-count")
```

### 场景覆盖对照表

| examples/ | 目标接口落点 | 关键变化 |
|---|---|---|
| 01_quickstart_table | 片段 1 | `write` 唯一动词；`with`；where 文本示范 `==`/`between` |
| 01_quickstart_timeseries | 片段 3 | `pk` 别名；废弃参数警告 |
| 02_calendar_events(_table) | 片段 4 上 | `query(date=)` 无需 timeseries；`timezone=` 统一 |
| 03_buffered_ingest | 片段 4 中 | 不变（已达标） |
| 04_layout_report | 片段 6 | `with_partition(partition=...)` 单签名 |
| 05_partition_segments | 片段 1+4 | `write` 替代 `insert_all`；其余不变 |
| 06_row_segment_late_data | 片段 4 中 | 同上 |
| 07_string_repartition_cli | 片段 6 + CLI | 字符串 partition 示范 `timezone=`（`tz=` 兼容） |
| 08_doctor | 片段 6 + CLI | `db.table()` 快捷参数替代手写 `TableContract` ★ |
| 09_existing_append | 片段 5 | 删防御代码；错误即指引 |
| 10_existing_repartition | 片段 5+6 | 删防御代码；`select()` 免样板 |
| 11/12 postgresql | 片段 7 | 不变 |
| finance/01 kline | 片段 2+3 | `latest(by=)`；timeseries 糖 |
| finance/02 orderbook | 片段 3+4 | write_buffer 不变；废弃参数警告 |
| finance/03 reports | 片段 4 上 | calendar 场景迁移到 `Partition.calendar` 显式形式 |
| finance/04 news | 片段 2 | `op="=="` 统一 |

## 变更记录

- 2026-08-22 合并稿：逐条核实前作 A/B 的全部关键声明（含运行时探针），修正前作 B 三处失实（dict op between 容错、partition 时区词汇统一表述、薄封装代码可行性），补充前作 B 遗漏的 7 项问题，输出修正后的接口设计建议与测试设计。
- 2026-08-22 实施前修订：P1-3 升级为"静默失效链"（op 拼错 → Case 静默通过 → 裁剪静默失效，探针证实）；新增 P1-6 evolve 语义契约未定义、P1-7 导出面过宽与 Case 魔法运算符、P1-8 with_partition 双签名；接口设计新增第八节（evolve 三语义）、第九节（导出面分层与 columns 别名）；开放问题补 4/5；新增"范围决策记录"（SQLite 优先、后端中立词汇、不含 auto-partition）。本版为 PLAN-052 的实施依据。
- 2026-08-22 补充：新增第八节"期望的用户接口"（目标形态代码片段 + examples 全场景覆盖对照表），作为 PLAN-052 的可执行验收基准。
