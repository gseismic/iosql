# HANDOFF — PLAN-054 用户接口定稿（进行中，未完成）

> **交接说明（2026-08-23）**：PLAN-054（用户接口定稿，长期稳定）已实施代码部分 N1~N9 +
> README 更新（N5/N11），**尚未完成**：全量回归确认、示例兼容性检查、OUTCOME 文档、
> git 提交。当前工作区有 10 个已修改文件 + 2 个未跟踪文档，**未提交**。
> 接手者请先读本文档，再读计划/设计文档，从"下一步做什么"开始。

---

## 1. 任务是什么

用户要求：v0.3.0（PLAN-052/053 已实施）后，对用户接口做**最终定稿**——这次定出的接口
要能稳定、长期不变。评审按三标准进行（一致性/低理解负担/渐进复杂），评审结论与冻结
边界见设计文档。

- **设计文档（必读）**：`docs/design/user-api-review-20260823-followup.md`
  —— 含 N1~N10 问题清单、"接口定稿"章节（冻结边界、最小实施集合 11 项）。
- **实施计划**：`docs/dev/PLAN-054-user-api-finalize.md` —— 11 项实施项 + 验收标准 + 不做清单。
- 按 AGENTS.md 规范：实施完成后生成 `docs/dev/PLAN-054-user-api-finalize-OUTCOME.md`，
  中文 commit（消息记录 PLAN 与 OUTCOME 文件），docs/ 目录要提交。

## 2. 当前系统状态

- 版本：`iosql/version.py` `__version__ = '0.3.0'`（PLAN-052 已发布）
- 最近提交：`2b5fa23`（PLAN-053 review 修复）；当前 HEAD 之上有未提交的 PLAN-054 改动
- 全量测试基线（PLAN-053 OUTCOME）：`345 passed, 16 skipped`

## 3. 已完成（代码已改，未提交）

工作区改动（`git diff --stat`）：
```
 README.md                                        | 26 +++++
 iosql/__init__.py                                | 10 +---
 iosql/core/query/case.py                         |  2 +-
 iosql/engines/postgresql/table.py                |  4 +-
 iosql/engines/sqlite/table.py                    | 41 +++++--
 iosql/partition/duration.py                      | 64 ++++++---
 iosql/spec.py                                    | 57 +-------
 tests/engines/sqlite/test_classic_use.py         |  3 +-
 tests/engines/sqlite/test_timeseries_contract.py |  3 +-
 tests/test_user_api_unify.py                     |  3 +-
```
未跟踪（需 git add）：
```
 docs/design/user-api-review-20260823-followup.md
 docs/dev/PLAN-054-user-api-finalize.md
```

### 逐项实施状态

| 项 | 状态 | 变更内容 |
| --- | --- | --- |
| N2 统一间隔词汇表 | ✅ 已实施 | `iosql/partition/duration.py` 新增模块函数 `parse_interval_ms()`（通用解析：别名 + 任意 `Nunit`）；`DurationPartition.__init__` 改用它；`iosql/spec.py` 的 `_duration_to_ms` 改为委托 `parse_interval_ms`（删除重复实现，spec.py 净减约 50 行）。效果：`Partition.duration(every='1s')` 与 `timeseries(partition_every='1s')` 行为一致。注意 `parse_interval_ms` 从 `iosql.partition.duration` 导入，spec.py 已加对应 import（无循环导入）。 |
| N9 修 Case.__repr__ | ✅ 已实施 | `iosql/core/query/case.py` `__repr__` 补右括号，字段加 `!r` 引号：`Case(field='sym', op='==', value={...})` |
| N6 write_buffer 异常 warn | ✅ 已实施 | `iosql/engines/sqlite/table.py` `_BufferedWriter.__exit__`：异常且 buffer 非空时 `warnings.warn(..., RuntimeWarning)` 提示未 flush 行数 |
| N4 select 默认 to_dict=True | ✅ 已实施 | `iosql/engines/sqlite/table.py` `select`/`iter_select`/`iter_select_batches` 默认 `to_dict=True`（与 PG 引擎一致）；`to_dict=False` 保留返回 sqlite3.Row 元组。**破坏性变更**：依赖默认 tuple 行的调用方需显式 `to_dict=False`。`select_all`（已废弃）保持 `to_dict=False` 签名不变但内部强制 True。 |
| N5 PG count_rows 去参 | ✅ 已实施 | `iosql/engines/postgresql/table.py` `count_rows(where=None)` → `count_rows()`（无调用方传 where，内部全部无参调用） |
| N1 构造器导出收缩 | ✅ 已实施 | `iosql/__init__.py` 不再导出 `DurationPartition`/`CalendarPartition`/`RangePartition`（从 `__all__` 和 import 移除）；仅保留工厂 `Partition.*`。**已同步修改 2 处测试导入**：`tests/engines/sqlite/test_classic_use.py` 改为 `from iosql.partition.duration import DurationPartition`；`tests/engines/sqlite/test_timeseries_contract.py` 改为 `from iosql.partition.calendar import CalendarPartition`。**注意**：若还有别处 `from iosql import DurationPartition` 会报 ImportError，全量回归时留意。 |
| N3 symbol_column 默认显式化 | ✅ 已实施 | `iosql/spec.py` `TableContract.symbol_column: str = "symbol"` → `Optional[str] = None`；`to_dict` 条件从 `!= "symbol"` 改为 `is not None`；`from_dict`/`from_table_meta` 默认改 None。`iosql/engines/sqlite/table.py` `_latest_rows` 加防护：symbol_column 为 None 且未传 `by=` 时抛 BadArgument（含修复指引）；`_compile_query_where` 的 `symbols` 分支从 `or 'symbol'` 兜底改为 None 时抛 BadArgument。`timeseries()` 保持显式 `symbol_column='symbol'` 默认。**已同步修改 3 处测试**：`test_timeseries_equivalent_to_table_contract`（显式 table 加 symbol_column="symbol"）、`test_table_shortcut_time_and_session_column`（加 symbol_column="symbol"）、`test_raw_table_spec_writes_queries_and_partitions_timeseries`（TableSpec 加 symbol_column="symbol"）。 |
| N8 explain_select 补业务参数 | ✅ 已实施 | `iosql/engines/sqlite/table.py` `explain_select` 新增 `symbols/start/end/date/session` 参数（透传 QuerySpec），与 `query().explain()` 能力对齐 |
| N7 upsert_all docstring 标注 | ✅ 已实施 | `iosql/engines/sqlite/table.py` `upsert_all` docstring 增加"O(全表)+Segment.rows 不兼容+推荐 write(mode='upsert')"警告 |
| N5/N11 README 更新 | ✅ 已实施 | `README.md` 新增两节：`partition 一词三义对照表`、`双引擎支持面`（SQLite vs PostgreSQL 能力对照，PG 定位"迁移目标+基础查询"） |
| 测试修复 | ✅ 已实施 | 见 N1/N3 行，共改 3 个测试文件（5 处断言/导入） |

### 已验证（局部）

- 单点验证全部通过：N2 间隔等价、N9 repr、N6 warn、N4 默认 dict、N1 导入收缩、
  N3 symbol_column None + latest 防护、N8 explain 与 query.explain 裁剪一致。
- 局部回归通过：`tests/test_user_api_unify.py` + `tests/engines/sqlite/test_timeseries_contract.py`
  + `tests/engines/sqlite/test_classic_use.py` → **75 passed**。

## 4. 下一步做什么（按顺序）

1. **全量回归**（必做，工作区改动大）：
   ```bash
   python -m pytest tests/ -q
   ```
   预期基线 `345 passed, 16 skipped`。重点留意：
   - N1 收缩导出后是否有遗漏的 `from iosql import DurationPartition/CalendarPartition/RangePartition`
     （已改 2 处测试，其余代码/示例若引用需改内部路径）。
   - N4 默认 to_dict=True 是否有测试断言默认 tuple 行。
   - N3 symbol_column=None 是否有测试 `query(symbols=)`/`latest(symbols=)` 未声明 symbol_column。
2. **跑 examples 全量**（README 说 select 默认 dict，示例里显式 to_dict=True 冗余但无害，确认不报错）：
   ```bash
   for f in examples/*.py; do python "$f" || echo "FAIL: $f"; done
   PYTHONPATH=. python examples/finance/01_kline_bars.py
   ```
3. **lsp_diagnostics** 检查改动的 6 个源文件（duration.py/spec.py/case.py/sqlite table.py/
   postgresql table.py/__init__.py）零新增错误。
4. **补测试**（若全量回归暴露缺口）：
   - N2：`Partition.duration(every='1s')` 与 `timeseries(partition_every='1s')` 契约等价。
   - N3：`db.table(...)` 不传 symbol_column → `spec().symbol_column is None`；
     `latest(symbols=)` 无 symbol_column 时报 BadArgument。
   - N4：`select()` 默认 dict 行；`select(to_dict=False)` 返回 tuple。
   - N6：write_buffer 块内异常发 RuntimeWarning。
   - N8：`explain_select(symbols=..., start=..., end=...)` 与 `query(...).explain()` scanned_partitions 一致。
5. **写结果文档**：`docs/dev/PLAN-054-user-api-finalize-OUTCOME.md`（按 AGENTS.md 规范，
   记录逐项变更、验证结果、已知限制）。
6. **提交**（中文 commit，记录 PLAN 与 OUTCOME 文件，逐条列变更；docs/ 要提交）：
   ```bash
   git add README.md iosql/ tests/ docs/design/user-api-review-20260823-followup.md docs/dev/PLAN-054-user-api-finalize.md docs/dev/PLAN-054-user-api-finalize-OUTCOME.md
   git commit -m "实施 PLAN-054: 用户接口定稿（长期稳定收敛）
   计划: docs/dev/PLAN-054-user-api-finalize.md
   结果: docs/dev/PLAN-054-user-api-finalize-OUTCOME.md
   逐条变更：..."
   ```

## 5. 冻结边界（本次定稿的承诺，后续不得违反）

定稿后**不再改参数名、默认值、方法职责**；新增能力只能以"新方法/新参数"叠加（默认行为=旧行为）。

- 参数词汇表唯一拼写：`pk` / `columns` / `every` / `timezone` / `between` / `mode`；
  别名永久保留：`primary_key` / `fields` / `partition_every` / `tz` / `between_and` / 契约侧 `conflict`。
- 缩写原则：只允许行业级缩写（`pk` 是唯一成员，依据 SQLAlchemy/Django/sqlite-utils 惯例）；
  其余参数一律全称（`timezone` 而非 `tz`）。
- 结果对象字段冻结，`to_dict()` 唯一序列化入口，新字段只能追加。
- 废弃参数只加不减（已废弃的 partition_kind/time_unit/*_rows_per_file/group_by/select_all
  永久保留 DeprecationWarning，不再计划移除）。
- 双引擎对称性：PG 引擎方法面不对称是设计意图（迁移目标+基础查询），保留并已文档化；
  但 `select` 默认值必须统一（本次已统一为 to_dict=True）、`count_rows` 签名已统一（无参）。

## 6. 明确不做（冻结边界之外）

- 不合并 `select()`/`query()`、不合并 `table()`/`timeseries()`
- 不引入结果对象替换 to_dict flag
- 不废弃 insert_all/upsert_all
- mysqlite 不纳入（第三方库定位）
- PG 引擎不补查询族（query/latest/asof 等）

## 7. 已知风险与注意

- **N4 是破坏性变更**（select 默认返回从 tuple 变 dict）：需在 OUTCOME 和 README 变更记录
  中显著标注，建议 bump 版本到 0.4.0（当前 0.3.0）。
- **N3 也是破坏性变更**（symbol_column 默认 None）：`query(symbols=)`/`latest(symbols=)`
  在未声明 symbol_column 的普通 `db.table()` 上会报错（带修复指引）——这是故意为之
  （消除隐藏默认），但调用方需显式声明。
- **N1 收缩导出**：`from iosql import DurationPartition` 现在 ImportError。内部模块
  `iosql.partition.duration` / `iosql.partition.calendar` / `iosql.partition.range`
  路径仍可用。
- 全量回归被中断过一次（当时用户急写交接，未跑完）；**接手第一步就是重跑全量**。
- 交接时 `git status` 显示 10 modified + 2 untracked，全部属于 PLAN-054，可直接提交。

## 8. 相关文档索引

- 设计（本次）：`docs/design/user-api-review-20260823-followup.md`
- 计划（本次）：`docs/dev/PLAN-054-user-api-finalize.md`
- 前序设计（v0.3.0 已实施）：`docs/design/user-api-unify-20260822-consolidated.md`
- 前序结果（v0.3.0）：`docs/dev/PLAN-052-user-api-unify-OUTCOME.md`
- 前序修复（v0.3.0 review）：`docs/dev/PLAN-053-v03-review-fixes-OUTCOME.md`