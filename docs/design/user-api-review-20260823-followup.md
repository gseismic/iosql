# user-api-review-20260823-followup

## 定位

v0.3.0（PLAN-052 用户接口统一 + PLAN-053 review 修复）实施后，基于 `examples/` 全量示例（14 主线 + 4 金融 + 3 mysqlite）与当前源码（`iosql/database.py`、`iosql/table.py`、`iosql/engines/sqlite/table.py`、`iosql/spec.py`、`iosql/core/query/*`）交叉比对的**二次接口审视**。评判框架沿用 `api-design-zh` skill（概念要少但要强、简单场景要短、危险操作显式化、命名一致性）。

与 `user-api-unify-20260822-consolidated.md`（v0.3.0 实施依据）的关系：本文只报告**实施后仍然存在**的接口问题；已在 v0.3.0 解决或已拍板决策的事项（timeseries 薄封装、query 反推 time、op 归一化、columns 别名、timezone= 统一、pk 推荐拼写、导出面分层等）不再重复。mysqlite 按第三方库定位排除。

## 验证方法

所有结论均经源码核对 + 运行时探针验证（见各条"实证"）。探针于 2026-08-23 在 Python 3.12.8 / iosql 0.3.0 上执行。

## 新增问题清单（v0.3.0 后仍存在）

### N1 · 工厂与构造器参数名系统性错位，且构造器对外导出（P0）

**事实**：`iosql/__init__.py` 同时导出工厂类方法 `Partition.duration/calendar/range` 与三个构造器类 `DurationPartition/CalendarPartition/RangePartition`。同一概念在工厂与构造器中使用不同参数名：

| 工厂（推荐路径） | 构造器（也导出） | 错位参数 |
| --- | --- | --- |
| `Partition.duration(column, every)` | `DurationPartition(interval_ms, ts_col)` | `every↔interval_ms`、`column↔ts_col` |
| `Partition.calendar(column, every, timezone)` | `CalendarPartition(fmt, tz, ts_col)` | `every↔fmt`、`timezone↔tz`、`column↔ts_col` |
| `Partition.range(column, every)` | `RangePartition(width, column)` | `every↔width` |

**实证**：`iosql/partition/{duration,calendar,range}.py` 构造器签名；`__init__.py:4-9` 导出。

**问题**：违反 `api-design-zh`"同一概念统一命名"。用户绕过工厂直接用构造器时，三组参数名全错位；README 从未示范构造器，但它们在 `from iosql import *` 可见——接口面大于文档面。属"暴露内部实现"反模式。

### N2 · 两套间隔解析词汇表（P0，行为不一致）

**事实**：`Partition.duration(every='1s')` 抛 `ValueError`，而 `timeseries(partition_every='1s')` 正常工作。

**实证**（探针 2026-08-23）：
```
duration(1s) FAIL: ValueError invalid literal for int() with base 10: '1s'
timeseries(1s) OK: PartitionSpec(column='ts', every=1000, kind='duration', time_unit='ms', timezone='UTC', fmt=None)
```

**根因**：`DurationPartition.__init__` 走 `INTERVAL_ALIASES` 白名单（无 `1s`/`3day` 等通用形式），而 `PartitionSpec.to_partition()` 走 `_duration_to_ms` 通用解析（`database.py:271-275` 注释自证"经 PartitionSpec 构造以支持 '1s'/'1day' 等字符串间隔"）。

**问题**：同一"分区周期"概念，两个入口（对象 API vs helper）接受不同间隔词汇。用户用 `Partition.duration(every='1s')` 失败后改用 `timeseries(partition_every='1s')` 却成功，行为不可预期。这是 `api-design-zh`"契约模糊"反模式的实例。

### N3 · 隐藏默认值：symbol_column 恒为 'symbol'（P1）

**事实**：`TableContract.symbol_column: str = 'symbol'`（spec.py:641），`timeseries(symbol_column='symbol')` 显式默认（database.py:200），`table(symbol_column=None)` 隐式 None（database.py:142）——但契约最终都落为 `'symbol'`。

**实证**（探针 2026-08-23）：不传 `symbol_column` 建普通表，`spec().symbol_column == 'symbol'`。

**问题**：用户的表没有 symbol 列时，契约也默默带上 `symbol_column='symbol'`。`latest(symbols=...)` 的语义依赖这个隐藏默认。违反"简单场景要短"的同时制造了隐性契约——用户以为没声明的东西其实被声明了。

### N4 · select 家族默认行格式与 query 家族不一致（P1）

**事实**：`select(..., to_dict=False)` 默认返回 `sqlite3.Row`（tuple 行），`query(...).to_list()` 恒返回 dict 行。

**实证**（探针 2026-08-23）：`select()` 默认行类型 `Row`（sqlite3.Row）。

**问题**：同一库两种默认行表示。`to_dict=True` 在 21 个示例中至少 10 处出现，说明 dict 才是用户的主流诉求；默认值反而制造样板代码。对照业界：sqlite-utils 默认 dict（Python 生态惯例），DuckDB/QuestDB 默认 tuple（DB-API 惯例）——iosql 是"一半一半"，两套都要记。v0.3.0 已把 `select()` 参数默认化，但 `to_dict` 默认值未一并收敛（PLAN-052 开放问题 2 倾向"结果对象风格"但未落实默认值）。

### N5 · "partition" 一词三义（P1）

**事实**：`partition` 参数在不同方法中含义不同：
- 建表：`db.table(partition=Partition.duration(...))` —— 分区策略对象
- 维护：`compact(partition=None)` / `cleanup_obsolete(partition=None)` —— 逻辑分区 key（int/str）
- 目标契约：`spec().with_partition(partition="duration(...)")` / `repartition_to(spec=...)` —— 字符串/对象

**问题**：同名参数跨方法类型漂移（对象 → key → 字符串），新用户无法从参数名推断期望类型。属命名一致性缺口。

### N6 · write_buffer 异常时静默丢弃缓冲行（P1，数据安全）

**事实**：`_BufferedWriter.__exit__` 仅在无异常时 flush（`engines/sqlite/table.py:220-225`）。

**问题**：`with table.write_buffer(...) as buf: buf.append(...)` 块内抛异常时，已 append 但未 flush 的行被静默丢弃，无警告无提示。违反 `api-design-zh`"危险操作必须显式化"——数据丢失是高风险副作用，应显式表达（至少 warning，或提供 `.discard_pending()` 显式接口）。

### N7 · upsert_all 性能陷阱未显式化（P1）

**事实**：`upsert_all` 实现为"全表 select + 内存合并 + replace 写回"（`engines/sqlite/table.py:3169`），O(全表)；且 segment 表必抛 `BadArgument`（走 replace 路径触发 `_ensure_segment_conflict_supported`）。

**问题**：方法名与 `insert_all` 对称，暗示"高效批量"；实际是性能陷阱 + segment 不兼容。风险未在命名或 docstring 外显式化。对照：`delete` 返回 int、`truncate` 返回 WriteResult，同族返回类型也不一致。

### N8 · explain_select 与 query().explain() 能力不对等（P2）

**事实**：`explain_select(fields, where, order_by, limit)`（table.py:706）无法表达 `symbols/start/end/date/session`；业务查询计划只能通过 `query(...).explain()` 获得。

**问题**：两个"获取查询计划"入口能力不对称。想解释 `query(symbols=...)` 的业务裁剪，必须绕道 fluent 对象；`explain_select` 只能解释底层 where。

### N9 · Case.__repr__ 缺右括号（P3，bug）

**事实**：`case.py:174` `f'Case(field={self.field}, op={self.op}, value={self.value}'` 无闭合括号。

**实证**（探针 2026-08-23）：`repr(Case('sym','==','A'))` 输出 `Case(field=sym, op===, value={'value': 'A', 'type': 'const'}`。

**问题**：调试输出不合法（`op===` 也难看），影响 REPL/日志可读性。

### N10 · 危险操作显式化已达标项（确认，不动作）

复核确认以下已达标，无需改动：
- `truncate(confirm=False)` 缺确认抛 `UnsafeOperation` ✓
- `cleanup_obsolete(dry_run=True, validate=True)` 默认 dry-run + 删除前深度校验 ✓
- `repartition_to` 要求不同目标 uri/表名 ✓
- CLI `repartition --replace-source --yes` 双重确认 ✓

## 已决策/已实施项（确认，不重复）

以下 v0.3.0 已解决或已拍板，本文不展开：
- `timeseries()` 薄封装 table()（PLAN-052 已实施，database.py:198-301）
- query() 便捷参数下放普通分区表（partition 反推 time，已实施）
- op 词表归一化 + 非法 op 即时报错（PLAN-052，case.py:27-35）
- `columns=` 别名（table.py:6-14，fields 兼容保留）
- partition 时区词汇统一 `timezone=`（`tz=` 兼容）
- `pk=` 推荐拼写（`primary_key=` 兼容）
- 导出面分层（README 用户层/高级层）
- 废弃参数迁移（partition_kind/time_unit/*_rows_per_file/group_by/select_all → DeprecationWarning）
- mysqlite 按第三方库定位（本设计不涉及）

## 三标准评审（一致性 / 低理解负担 / 渐进复杂）

按用户给出的理想库三标准，对 N1-N10 归位评估：

### 一致性（同一概念同一命名、同一语义同一表达）

| 问题 | 一致性损害 | 判定 |
| --- | --- | --- |
| N1 工厂↔构造器参数错位 | 同一"分区周期"在工厂叫 every、在构造器叫 interval_ms/width | 违反 |
| N2 两套间隔词汇表 | 同一概念两个入口接受不同词汇，行为不一致 | 违反（探针坐实） |
| N4 select 默认 Row vs query 恒 dict | 同一库两种默认行表示 | 违反 |
| N5 "partition" 一词三义 | 同名参数跨方法类型漂移 | 违反 |
| N7 delete 返回 int vs truncate 返回 WriteResult | 同族方法返回类型不一致 | 违反 |
| N8 explain_select 与 query().explain() 能力不对等 | 同一"查计划"意图两条路径能力不同 | 违反 |

### 低理解负担（默认值符合心智、隐藏默认少、接口面≤文档面）

| 问题 | 理解负担来源 | 判定 |
| --- | --- | --- |
| N1 导出面>文档面 | 构造器可见但文档不示范，用户不知道哪个是正统 | 违反 |
| N3 symbol_column 隐藏默认 'symbol' | 用户以为没声明的东西被默默声明 | 违反（探针坐实） |
| N6 write_buffer 异常静默丢弃 | 数据丢失无提示 | 违反（探针坐实） |
| N9 Case.__repr__ 缺右括号 | 调试输出不合法 | 违反（探针坐实） |

### 渐进复杂（简单场景 3-5 行、默认覆盖常见、参数分层）

**已达标（正向确认，不动作）**：
- `Segment.rows()` 零参数即用（默认 500k/500k/500k），完整参数渐进展开 ✓
- where 三形式天然渐进：文本（最简单）→ dict（常用）→ list（复杂）✓（探针：三形式均正常解析）
- `write()` → `write_buffer()` → `insert_all()` 三层写入渐进 ✓
- `db.table()` 15 参数全可选，`db.table("name")` 重开零仪式 ✓

**不足**：
- N4 的 `to_dict=True` 高频样板破坏"简单场景要短"（21 示例 10+ 处显式传）。
- N7 upsert_all 的名字暗示高效，实际 O(全表)——渐进路径上放了一个陷阱。

## 处理建议（我的裁决，按实施顺序）

### 第一批：纯 bug 修复（零破坏，立即做）

1. **N2 统一间隔词汇表**：`DurationPartition.__init__` 的 `INTERVAL_ALIASES` 白名单改为复用 `spec._duration_to_ms` 通用解析。一处实现两个入口行为一致。这是行为不一致 bug，修完 `Partition.duration(every='1s')` 与 `timeseries(partition_every='1s')` 等价。
2. **N9 修 `Case.__repr__`**：补右括号，op 输出加引号（`op='=='` 而非 `op===`）。
3. **N6 write_buffer 异常提示**：`__exit__` 捕获异常时对未 flush 行数发 `warnings.warn`（不改变 flush 语义）。数据丢失必须有声。

### 第二批：接口收敛（v0.4 主版本，兼容别名过渡）

4. **N1 收缩导出面**：`__init__.py` 不再导出三个构造器类（`DurationPartition/CalendarPartition/RangePartition`），保留工厂 `Partition.*`。**裁决：直接移除**——直接构造使用面窄（探针确认可 import 但 README 零示范），留着违反"接口面≤文档面"。CHANGELOG 标注。
5. **N3 symbol_column 默认显式化**：`table()` 不传时契约为 `None`；`timeseries()` 保持显式 `'symbol'`。**裁决：改为 None**——隐藏默认是隐性契约，`latest(symbols=)` 在无 symbol 列时应明确报错而非静默按 'symbol' 语义跑。
6. **N4 select 默认行格式**：`select(to_dict=True)` 改为默认，`to_dict=False` 兼容保留（tuple 行）。**裁决：翻转默认值**——对齐 sqlite-utils 惯例与示例主流用法；tuple 调用方需显式 `to_dict=False`，属可接受的 v0.4 破坏。
7. **N7 upsert_all 定位**：docstring 标注 O(全表)+segment 不兼容，推荐 `write(mode='upsert')`。**裁决：选项 A（标注+推荐）**——废弃并入 write 是破坏性变更且 write 已覆盖场景；真增量 upsert 依赖全局 pk index（远期），不阻塞。

### 第三批：能力对齐（增量，不破坏）

8. **N8 explain_select 能力对齐**：为 `explain_select` 补 `symbols/start/end/date/session` 参数（透传 QuerySpec），与 `query().explain()` 能力等价。纯增量，无破坏。
9. **N5 "partition" 一词三义**：**裁决：选项 B+C 组合**——文档建立"分区策略/分区键/目标分区"三词对照表；`compact(partitions=)`/`cleanup_obsolete(partitions=)` 增加 `partitions=` 别名（`partition=` 兼容保留）。改名（A）波及调用方且收益有限，文档澄清 + 别名过渡已足够。

## 测试设计

1. `Partition.duration(every='1s')` 与 `timeseries(partition_every='1s')` 产出的契约等价（spec().partition 一致）。
2. `repr(Case(...))` 以 `)` 结尾，op 输出 `==` 而非 `===`。
3. `__init__.py` 移除构造器导出后，`from iosql import DurationPartition` 报 ImportError（破坏性变更需 CHANGELOG 标注）。
4. `table()` 不传 symbol_column 时 `spec().symbol_column is None`；传 `symbol_column='sym'` 时正确绑定；`latest(symbols=)` 在无 symbol_column 时报可恢复错误。
5. `select()` 默认返回 dict 行；`select(to_dict=False)` 仍返回 tuple 行（兼容）。
6. `write_buffer` 块内抛异常时发出 Warning 且未 flush 行数可查。
7. `explain_select(symbols=..., start=..., end=...)` 与 `query(...).explain()` 的 scanned_partitions 一致。
8. `compact(partitions=...)` 与 `compact(partition=...)` 行为一致（别名回归）。
9. 既有 `test_user_api_unify.py` / `test_plan052_concurrency.py` 全量回归不回归。

## 实施顺序建议

```
第一批（bug 修复）: N2 → N9 → N6        # 零破坏，先行
第二批（v0.4 收敛）: N1 → N3 → N4 → N7   # 主版本，兼容别名
第三批（能力对齐）: N8 → N5              # 增量，随第二批复审
```

## 接口定稿（2026-08-23 冻结边界）

用户要求"这次定出的接口能稳定，长期不变"。以下为冻结决策：**定稿后不再改参数名、默认值、方法职责**；新增能力只能以"新方法/新参数"方式叠加，不允许改既有签名语义。

### 一、双引擎对称性（本次最大的定稿决策）

**现状事实**（探针 2026-08-23 核对源码）：
- SQLite 引擎有 15 个查询/维护方法：`query/latest/asof/explain_select/segments/compact/validate/cleanup_obsolete/layout_report/repartition_to/truncate/iter_select/iter_select_batches/iter_rows/count_rows`
- PG 引擎只有 6 个：`select/iter_select/iter_rows/count_rows/delete/upsert_all`（缺 query/latest/asof/explain_select/segments/compact/validate/cleanup_obsolete/layout_report/repartition_to/truncate）
- **`select` 默认值相反**：SQLite `to_dict=False`（返回 Row 元组），PG `to_dict=True`（返回 dict）

**裁决：**
1. **方法面不对称是设计意图，予以保留并文档化**。PG 引擎定位是"迁移目标 + 基础查询"，不是完整业务接口；`query/latest/asof` 等时序语义在 PG 侧无 native 实现基础。不补方法（避免伪实现），但在 README 显著标注"PG 引擎支持面 = 写 + select/iter_select/delete/count/upsert；时序便捷查询仅在 SQLite 侧"。
2. **`select` 默认值必须统一**（N4 落点）：翻转 SQLite `to_dict=False → True`，PG 保持 `True`。**这是"同一库同一方法同一默认值"的最低要求**，也是双引擎一致性里唯一必须修的。翻转后 SQLite/PG 行为一致。
3. `count_rows(where=None)`（PG 有 where，SQLite 无）——**统一为无 where**：PG 移除 `where` 参数（该参数在 PG 侧也无实际用户，`count_rows(where)` 是迁移校验内部路径，可改走 `select` 计数）。避免同名方法签名分叉。

### 二、定稿参数词汇表（冻结）

以下拼写为唯一推荐，其余为兼容别名（**别名长期保留，永不移除**）：

| 概念 | 唯一拼写 | 兼容别名 |
| --- | --- | --- |
| 主键 | `pk=` | `primary_key=` |
| 字段选择 | `columns=` | `fields=` |
| 写入模式 | `write(mode=)` | 契约侧 `conflict=`（读侧） |
| 分区周期 | `every=` | `partition_every=`（仅 timeseries 便捷参数） |
| 时区 | `timezone=` | `tz=` |
| 查询字段 | `query(columns=)` | — |
| 结果行 | `select()` 默认 dict | `to_dict=False` 保留 tuple |
| 区间 op | `between` | `between_and`/`range`（内部） |

**契约侧 `conflict=` 与调用侧 `mode=` 保留双名**（已拍板 PLAN-052：`mode` 是写入动词的自然搭配，契约 `conflict` 是声明字段；文档明确两者关系）。

#### 缩写原则（为什么 `pk` 简称、`timezone` 全称）

用户质疑"pk 用简称、timezone 不用简称"不对称。定稿裁决如下——**缩写不是逐参数拍的，而是"行业级缩写白名单"**：

**规则：只允许 Python 数据库生态中成为事实标准的缩写进入推荐拼写；`pk` 是白名单内唯一成员，其余参数一律全称。**

依据：
1. **`pk` 是行业事实标准**：SQLAlchemy / Django ORM / sqlite-utils / peewee / TinyDB 的建表与查询接口全部使用 `pk` 作为 primary key 的公开参数名。用户预期 `pk=` 存在，`pk` 在数据库语境下零歧义。
2. **`timezone` 全称与标准库对齐**：Python stdlib `datetime.timezone`、SQLAlchemy `timezone=` 参数、arrow 均用全称；`tz` 在 pandas 等库中虽也用，但作为**参数名**的行业通用度低于全称，且在字符串分区表达式 `calendar(date,every=1day,timezone=Asia/Shanghai)` 中全称可读性显著更好（`tz=Asia/Shanghai` 像拼写错误）。
3. **`tz` 降为兼容别名**：解析层继续接受 `tz=`（已有实现），但不作为推荐拼写进入任何新文档/示例。
4. **边界清晰**：除 `pk` 外，`symbol_column`/`session_column`/`order_key`/`batch_size`/`include_obsolete` 等一律全称，不存在第二个缩写成员。若未来确有第二个行业级缩写（如 `db` 已是类型名），需先过本原则评审再入白名单。

**判定工具**：新增参数命名时问——"这个缩写是否在 SQLAlchemy/Django/sqlite-utils 的公开接口中成为唯一写法？"是→可缩写；否→全称。

### 三、定稿结果对象（冻结）

- 全部结果类型（WriteResult/QueryPlan/LayoutReport/CompactResult/CleanupResult/RepartitionResult/MigrationResult/SegmentValidationResult/ValidationIssue/TableMigrationResult）字段**冻结**。
- `to_dict()` 是唯一序列化入口，**新增字段只能追加**，不允许改名/删字段。
- `WriteResult.rows_written` 的 `int|None`（ignore 模式 None）语义冻结为"未写入时 None"——文档化，不改变。

### 四、定稿后能力扩展规则

1. **新增能力 = 新方法/新参数**，不改变既有签名语义。
2. 新参数必须带默认值且默认行为=旧行为（向后兼容叠加）。
3. 废弃参数**只加不减**：新加废弃参数需要 v+1 版本窗口，且移除动作需再等一个主版本。当前已废弃的（partition_kind/time_unit/*_rows_per_file/group_by/select_all）**定稿为永久保留 DeprecationWarning**，不再计划移除（避免反复变动）。

### 五、本次定稿要求实施的最小集合

1. **N2 统一间隔词汇表**（bug）：`DurationPartition` 复用 `_duration_to_ms`。
2. **N9 修 `Case.__repr__`**（bug）。
3. **N6 write_buffer 异常 warn**（数据安全）。
4. **N4 `select` 默认 `to_dict=True`**（双引擎统一，SQLite 翻转）。
5. **PG `count_rows` 移除 `where` 参数**（双引擎签名统一）。
6. **N1 构造器导出收缩**（接口面=文档面）。
7. **N3 `symbol_column` 默认显式化**（`table()` 不传 = None）。
8. **N8 `explain_select` 补 symbols/start/end/date/session**（能力对齐）。
9. **N7 `upsert_all` docstring 标注 O(全表) + 推荐 write(mode='upsert')**。
10. **N5 "partition" 一词三义文档对照表**（不改名，文档澄清）。
11. **README 双引擎支持面对照表**（PG 子集声明）。

### 六、明确不做的（冻结边界之外）

- **不合并 `select()` 与 `query()`**：两个查询路径保留（PLAN-052 已拍板），文档标注差异。
- **不合并 `table()` 与 `timeseries()`**：语法糖保留（入门友好）。
- **不引入结果对象替换 to_dict flag**：`to_dict` 标志长期保留（PLAN-052 开放问题 2 已倾向此方案）。
- **不废弃 insert_all/upsert_all**：保留为低层接口，文档标注层级。
- **mysqlite 不纳入**：第三方库定位，接口独立演进。
- **PG 引擎不补查询族**：方法面不对称是意图，文档化而非实现。

## 变更记录

- 2026-08-23 初稿：v0.3.0 实施后二次审视。基于 examples/ 全量示例 + 源码核对 + 运行时探针，产出 N1-N10 问题清单、三档修复方向、测试设计与开放问题。N2/N3/N4/N9 均有探针实证。
- 2026-08-23 修订一：按用户三标准（一致性/低理解负担/渐进复杂）归位评审 N1-N10；渐进复杂维度正向确认已达标项（Segment 渐进、where 三形式、write 三层、table 零仪式）；对全部 9 项给出明确处理裁决（不再是开放选项），分三批实施顺序。
- 2026-08-23 修订二（接口定稿）：应用户"长期稳定"要求，新增"接口定稿"章节冻结边界——双引擎对称性裁决（PG 子集保留 + select 默认值统一 + count_rows 签名统一）、定稿参数词汇表、结果对象冻结规则、能力扩展规则、最小实施集合 11 项、明确不做 6 项。