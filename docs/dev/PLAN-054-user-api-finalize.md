# PLAN-054: 用户接口定稿（v0.3.0 后最终收敛）

日期: 2026-08-23
来源: docs/design/user-api-review-20260823-followup.md 接口定稿章节（三标准评审：一致性/低理解负担/渐进复杂）

## 目标

应用户"这次定出的接口能稳定，长期不变"要求，实施定稿最小集合 11 项。定稿后不再改参数名、默认值、方法职责；新增能力只能以新方法/新参数叠加。

## 实施项

### 1. N2 统一间隔词汇表（bug，sqlite/duration）

现象：`Partition.duration(every='1s')` 抛 ValueError（INTERVAL_ALIASES 白名单），`timeseries(partition_every='1s')` 正常（PartitionSpec 通用解析）。

方案：`DurationPartition.__init__` 复用 `spec._duration_to_ms` 通用解析；INTERVAL_ALIASES 作为快速路径保留。

验收：`Partition.duration('ts', every='1s')` 与 `timeseries(partition_every='1s')` 契约等价。

### 2. N9 修 `Case.__repr__`（bug）

现象：`case.py:174` `f'Case(field=..., op=..., value=...'` 缺右括号，op 输出 `===`。

方案：补右括号，op 输出加引号（`op='=='`）。

验收：`repr(Case('sym','==','A'))` 以 `)` 结尾，含 `op='=='`。

### 3. N6 write_buffer 异常时 warn（数据安全）

现象：`_BufferedWriter.__exit__` 仅在无异常时 flush（table.py:220-225），块内异常时缓冲行静默丢弃。

方案：`__exit__` 捕获异常时对未 flush 行数发 `warnings.warn`。

验收：块内抛异常时发出 Warning 含未 flush 行数。

### 4. N4 select 默认 to_dict=True（双引擎统一）

现象：SQLite `select(..., to_dict=False)` 返回 Row 元组，PG `to_dict=True` 返回 dict——同一方法双引擎默认相反。

方案：SQLite `select` 默认 `to_dict=True`；`to_dict=False` 兼容保留 tuple 行。PG 保持 True。

验收：`select()` 默认返回 dict 行；`select(to_dict=False)` 返回 tuple。

### 5. PG count_rows 移除 where 参数（双引擎签名统一）

现象：PG `count_rows(where=None)`，SQLite `count_rows()` 无参。

方案：PG `count_rows` 移除 `where` 参数（无实际用户，迁移校验走 select 计数）。

验收：PG `count_rows()` 无参可用；签名与 SQLite 一致。

### 6. N1 构造器导出收缩（接口面=文档面）

现象：`__init__.py` 导出 `DurationPartition/CalendarPartition/RangePartition` 构造器类，参数名与工厂错位。

方案：`__init__.py` 不再导出三个构造器类，保留工厂 `Partition.*`。

验收：`from iosql import DurationPartition` 报 ImportError；`from iosql import Partition` 正常。

### 7. N3 symbol_column 默认显式化

现象：`TableContract.symbol_column: str = 'symbol'` 恒有隐藏默认。

方案：`table()` 不传时契约为 None；`timeseries(symbol_column='symbol')` 保持显式默认。

验收：`db.table(...)` 不传 symbol_column 时 `spec().symbol_column is None`。

### 8. N8 explain_select 补 symbols/start/end/date/session

现象：`explain_select(fields, where, order_by, limit)` 无法表达业务时间查询，与 `query().explain()` 能力不对等。

方案：`explain_select` 补 `symbols/start/end/date/session` 参数（透传 QuerySpec）。

验收：`explain_select(symbols=..., start=..., end=...)` 与 `query(...).explain()` 的 scanned_partitions 一致。

### 9. N7 upsert_all docstring 标注

现象：`upsert_all` O(全表) 扫描 + segment 表不兼容，名字暗示高效。

方案：docstring 标注 O(全表) + segment 不兼容 + 推荐 `write(mode='upsert')`。

验收：docstring 含上述标注。

### 10. N5 partition 一词三义文档对照表

现象：`partition` 参数在建表（策略对象）/维护（逻辑 key）/重分区（字符串）三处含义不同。

方案：README 新增"分区策略/分区键/目标分区"三词对照表（不改名）。

### 11. README 双引擎支持面对照表

现象：PG 引擎只有 6 个用户方法，缺查询/维护族 11 个；用户无文档依据。

方案：README 新增 SQLite/PG 支持面对照表，标注 PG 为"迁移目标 + 基础查询"。

## 不做（冻结边界）

- 不合并 select/query、table/timeseries
- 不引入结果对象替换 to_dict
- 不废弃 insert_all/upsert_all
- mysqlite 不纳入
- PG 不补查询族
- 废弃参数永久保留 DeprecationWarning（不移除）

## 实施顺序

N2 → N9 → N6（bug 修复）→ N4 → N5(PG count_rows) → N1 → N3 → N8 → N7(docstring) → N10/N11(README) → 全量回归 → 自审 → OUTCOME