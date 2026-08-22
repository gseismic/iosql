# PLAN-054 结果: 用户接口定稿（v0.3.0 后最终收敛）

日期: 2026-08-23
计划: docs/dev/PLAN-054-user-api-finalize.md
状态: COMPLETE（11/11 实施项完成，全量回归通过）

## 变更清单

### N2 统一间隔词汇表（iosql/partition/duration.py + iosql/spec.py）
- 新增模块函数 `parse_interval_ms()`：统一解析别名（INTERVAL_ALIASES）与任意 `Nunit`
  形式（ms/s/min/hour/day/week），错误统一抛 `PartitionSpecError`。
- `DurationPartition.__init__` 改用 `parse_interval_ms`；`spec._duration_to_ms` 改为
  委托 `parse_interval_ms`（删除重复实现，spec.py 净减约 50 行）。
- 效果：`Partition.duration(every='1s')` 与 `timeseries(partition_every='1s')` 行为一致
  （此前前者抛 ValueError）。

验证：`Partition.duration('ts', every='1s')` → interval_ms=1000；`every='3day'` → 259200000；
`timeseries(partition_every='1s')` 契约等价；非法周期 `'xyz'` 抛 PartitionSpecError。

### N9 修 Case.__repr__（iosql/core/query/case.py）
- `__repr__` 补右括号，字段加 `!r` 引号。

验证：`repr(Case('sym','==','A'))` → `Case(field='sym', op='==', value={'value': 'A', 'type': 'const'})`。

### N6 write_buffer 异常 warn（iosql/engines/sqlite/table.py）
- `_BufferedWriter.__exit__`：异常且 buffer 非空时发 `warnings.warn(..., RuntimeWarning)`
  提示未 flush 行数；正常路径 flush 语义不变。

验证：块内抛异常 → RuntimeWarning 触发（含未 flush 行数）；正常退出无 warn。

### N4 select 默认 to_dict=True（iosql/engines/sqlite/table.py）
- `select`/`iter_select`/`iter_select_batches` 默认 `to_dict=True`，与 PG 引擎一致；
  `to_dict=False` 保留返回 sqlite3.Row 元组。`select_all`（已废弃）签名不变。

验证：`select()` 默认 dict 行；`select(to_dict=False)` 返回 Row；iter 族默认 dict。

### N5 PG count_rows 移除 where 参数（iosql/engines/postgresql/table.py）
- `count_rows(where=None)` → `count_rows()`，与 SQLite 引擎签名统一（无调用方传 where）。

验证：签名无参；内部 `migrate_to` 校验路径全部无参调用。

### N1 构造器导出收缩（iosql/__init__.py）
- 移除 `DurationPartition`/`CalendarPartition`/`RangePartition` 的顶层导出，仅保留工厂
  `Partition.*`；内部路径 `iosql.partition.{duration,calendar,range}` 仍可用。
- 同步修改 2 处测试导入（test_classic_use.py、test_timeseries_contract.py）。

验证：`from iosql import DurationPartition` → ImportError；`from iosql import Partition` 正常；
`from iosql.partition.duration import DurationPartition` 正常。

### N3 symbol_column 默认显式化（iosql/spec.py + iosql/engines/sqlite/table.py）
- `TableContract.symbol_column` 默认 `"symbol"` → `None`；`to_dict`/`from_dict`/
  `from_table_meta` 同步。
- `_latest_rows` 防护：symbol_column 为 None 且未传 `by=` 时抛 BadArgument（含修复指引）。
- `_compile_query_where` symbols 分支：None 时抛 BadArgument（原 `or 'symbol'` 兜底移除）。
- `timeseries()` 保持显式 `symbol_column='symbol'` 默认。
- 同步修改 3 处测试（test_user_api_unify.py ×2、test_timeseries_contract.py ×1）。

验证：`db.table(...)` 不传 → `spec().symbol_column is None`；显式传 → 正确绑定；
timeseries 保持 'symbol'；契约 to_dict/from_dict 往返稳定；`latest(symbols=)` 无
symbol_column 报 BadArgument；`latest(by=)` 可用。

### N8 explain_select 补业务参数（iosql/engines/sqlite/table.py）
- `explain_select` 新增 `symbols/start/end/date/session` 参数（透传 QuerySpec），
  与 `query().explain()` 能力对齐。

验证：`explain_select(symbols='A', start=..., end=...)` 与 `query(...).explain()`
scanned_partitions 一致；date 参数裁剪正确。

### N7 upsert_all docstring 标注（iosql/engines/sqlite/table.py）
- docstring 增加"O(全表)+Segment.rows 不兼容+推荐 write(mode='upsert')"警告。

### N10/N11 README 更新（README.md）
- 新增 `partition 一词三义对照表`（建表=分区策略 / 维护=分区键 / 重分区=目标策略）。
- 新增 `双引擎支持面` 对照表（SQLite 完整 vs PG 迁移目标+基础查询；select 默认值已统一）。

## 验证结果

- 全量回归：`345 passed, 16 skipped`（与基线一致，零失败）。
- 局部回归：test_user_api_unify + test_timeseries_contract + test_classic_use → 75 passed。
- 静态检查：Pyright 诊断行号全部落在本次 diff 区域之外（存量诊断类别与基线对应），
  本次变更零新增诊断。
- 破坏性变更：N4（select 默认 tuple→dict）、N3（symbol_column 默认 None）、
  N1（构造器顶层导出移除）——建议 bump 版本 0.4.0。

## 已知限制

- N3 后，未声明 symbol_column 的普通 `db.table()` 上 `query(symbols=)`/`latest(symbols=)`
  会抛 BadArgument（带修复指引）——这是消除隐藏默认的故意行为。
- N4 后依赖 select 默认 tuple 行的调用方需显式 `to_dict=False`。
- PG 引擎方法面不对称（缺 query/latest/asof/explain_select/compact 等）为设计意图，
  已在 README 双引擎支持面章节文档化，不补实现。

## 冻结边界（后续不得违反）

- 参数词汇表唯一拼写：`pk`/`columns`/`every`/`timezone`/`between`/`mode`；别名永久保留。
- 缩写原则：仅 `pk` 为行业级缩写（SQLAlchemy/Django/sqlite-utils 惯例），其余全称。
- 结果对象字段冻结，`to_dict()` 唯一序列化入口，新字段只能追加。
- 废弃参数只加不减（已废弃项永久保留 DeprecationWarning）。
- 双引擎：select 默认值已统一；count_rows 签名已统一；PG 方法面不对称保留并文档化。