# PLAN-052 用户接口统一与稳定（P0）

## 背景

依据 `docs/design/user-api-unify-20260822-consolidated.md`（实施前修订版），用户接口层存在概念碎片化：`timeseries()` 独立词汇表、`query()` 便捷参数不覆盖普通分区表、`select()` 必传位置参数、op 拼写不一致且存在**静默失效链**（拼错 op → `Case` 构造静默通过 → 分区裁剪静默失效）、evolve 动态扩列语义未定义、导出面过宽。

范围决策（2026-08-22 拍板，详见设计文档"范围决策记录"）：**本轮只做 SQLite 后端的接口稳定**；宽表列存后端（Parquet+DuckDB）推迟；不含 auto-partition（P1+，见 `docs/design/auto-partition-20260822-overview.md`）；全部新增接口词汇保持后端中立。

## 目标

1. `Database.table()` 补齐 `time` / `symbol_column` / `session_column` 参数，契约感知能力下沉主入口。
2. `timeseries()` 收敛为 `table()` 薄语法糖；废弃 `partition_kind` / `time_unit` / `*_rows_per_file`（DeprecationWarning，行为等价）；补 `pk` 别名。
3. `select()` / `iter_select()` 参数默认化（`fields="*"`、`where=None`）并增加 `columns=` 别名。
4. partition → time 反推（仅 duration/calendar），普通分区表获得 `query(date=...)` 等便捷查询。
5. op 归一化白名单进 `Case.__init__`：`between` 容错、非法 op 即时抛 `BadArgument`（含合法 op 列表与修复示例）——堵静默失效链。
6. evolve 三语义落地：列单调增长 + 旧分区 NULL 补齐、契约回写 `TableSpec.columns`、类型冲突 `BadArgument`。
7. 错误指引：`write()` 无契约错误附修复指引。
8. 示例与 README 收敛：统一 `write()`、`with Database(...)`、`op="=="`/`between`、`timezone=`；删除例 09/10 防御代码；导出面分层示范。
9. 弃用别名：`group_by`（latest）、`select_all`、`with_partition(every=)` 示范层收敛（实现保留兼容）。

## 非目标

- 不实现 auto-partition / `auto=True` / `layout_status()`。
- 不实现 `open_table()` 拆入口（开放问题 4，轻方案先行）。
- 不移除任何公开符号（全部走兼容别名 + DeprecationWarning）。
- 不改 PostgreSQL 引擎的既有行为（`table()` 新参数对 PostgreSQL 表原样存入契约即可）。
- 不动 `iosql.utils.mysqlite` 内部接口。

## 方案（任务块按依赖排序）

### T1 · `table()` 参数面补齐（前置，其他块依赖）

- `iosql/database.py` `table()` 新增 `time=None, symbol_column=None, session_column=None`，透传进 `TableContract`（字段已存在，仅参数面缺口）；与 `spec=` 互斥规则沿用现有 shortcut 检查。
- `timeseries()` 增加 `pk` 别名（与 `primary_key` 冲突检查，同 `table()` 现有逻辑）。
- 验收：`db.table(..., time="ts", session_column="trading_date", ...)` 与手写 `TableContract` 等价（`spec()` 一致）。

### T2 · `timeseries()` 薄封装 + 参数废弃

- 重写为委托 `self.table(...)`：duration 分支 `Partition.duration(time, every=partition_every)`（**无 timezone**，UTC 对齐）；`partition_kind="calendar"` 分支转 `Partition.calendar(time, every=..., timezone=...)`。
- `partition_kind` / `time_unit`：接受并触发 `DeprecationWarning`（信息给出新写法示例），行为等价转换。
- `target/min/max_rows_per_file`：触发 `DeprecationWarning`，内部映射为 `Segment.rows(target_rows=..., min_rows=..., max_rows=...)` 传入 `segment=`。
- 验收：设计文档测试 1/2/11；新签名与等价 `table()` 调用产出契约一致。

### T3 · 查询层接口稳定

- `engines/sqlite/table.py` + `engines/postgresql/table.py`：`select()` / `iter_select()` / `iter_select_batches()` 签名默认化 `fields="*", where=None`；增加 `columns=` 别名（`fields` 保留兼容，两者同传冲突时抛 `BadArgument`）。
- `spec.py` `TableContract.__post_init__`：`time is None` 且 partition 为 duration/calendar 时，从 partition 列反推 `TimeColumn`；`range` 不反推（文档化不对称）。显式 `time=` 优先。
- `query()` / `latest()` / `asof()` 在无 time 契约时：`MissingTimeColumn` 错误信息附修复指引（"建表时传 time= 或使用 duration/calendar 分区以自动推导"）。
- `iosql/core/query/case.py`：`__init__` 入口 op 归一化（`between`→`between_and`、`=`/`eq`→`==` 等别名表，白名单 = `_OP_SQL` ∪ 裁剪层已识别集，实施时以源码为准收齐）；非法 op 抛 `BadArgument`，信息含合法 op 列表与一条修复示例；`check_field_tvalue` 的裸 `assert` 一并替换为 `BadArgument`。
- `latest(group_by=)`：`DeprecationWarning`，指向 `by=`。
- `select_all`：`DeprecationWarning`，指向 `select(to_dict=True)`。
- 验收：设计文档测试 3/4/7/10/11。

### T4 · evolve 三语义

- 语义一（NULL 补齐）：跨分区查询 union 时，缺失列以 NULL 投影；实施时核对 `engines/sqlite/table.py` 多分区 SELECT 聚合路径（若已天然成立则补测试固化即可）。
- 语义二（契约回写）：动态加列成功后同步更新持久化 `TableSpec.columns`（核对 `insert_all` alter 路径是否已回写 meta；缺则补）。
- 语义三（类型冲突）：未声明列首次推断后固化；同列类型不兼容抛 `BadArgument`（含列名、已固化类型、本次类型、修复建议）。
- 验收：设计文档测试 9；README 注意事项新增"动态扩列语义"小节。

### T5 · 错误指引

- `engines/sqlite/table.py` `write()`：`'write requires a TableContract'` 扩展为含修复指引（重新声明参数或 `spec=` 绑定）。
- 验收：错误信息包含两条修复路径；例 09/10 改为直接调用（信任 API 错误路径），作为活体测试。

### T6 · 示例与 README 收敛

- 全部示例（含 finance/、mysqlite/ 不动）：统一 `write()`、`with Database(...)`、`op="=="` 与 `op="between"`、字符串 partition 示范 `timezone=`、`with_partition(partition=...)`。
- 例 08：`TableContract(...)` 手写改写为 `db.table(...)` 快捷参数等价形式（T1 后可行），原文标注"高级用法仍可直接构造契约"。
- README：合法 op 表、timeseries 薄封装说明与废弃参数迁移表、evolve 语义小节、导出面两层示范（用户层 / 高级层）、`query()` 与 `select()` 返回语义差异标注。
- 验收：`grep` 检查示例中不再出现 `between_and`、`op": "="`、`tz=` 示范、裸 `db.close()`（quickstart 类）；全部示例可运行。

### T7 · 回归与发布

- 全量 `pytest tests/`（Linux 基线 278 passed, 14 skipped；新增测试后数量上调）。
- 版本号 bump（新增参数与别名，minor）；更新 README 变更记录。
- 产出 `PLAN-052-user-api-unify-OUTCOME.md`（逐任务记录实施结果、测试数字、遗留项）。

## 实施顺序与依赖

```
T1 → T2 → T6(例08改写部分)
T3（独立，可并行）
T4（独立，可并行）
T5 → T6(例09/10部分)
T6 → T7
```

## 验证

1. 新增 `tests/test_user_api_unify.py`（或并入既有合适文件）覆盖设计文档测试设计 1-11 全部条目。
2. **验收基准**：设计文档第八节"期望的用户接口"——每个片段即验收用例；实施完成后逐片段核对可运行且行为一致，"场景覆盖对照表"逐行核对无遗漏。
3. 运行：
   - `pytest tests/`（全量回归）
   - `python examples/01_quickstart_table.py` 等全部示例逐一运行（T6 验收）
4. DeprecationWarning 用 `pytest.warns(DeprecationWarning)` 断言，确保弃用路径可被发现而非静默。
5. 提交按任务块拆分 commit（中文），最终 commit 消息记录 PLAN-052 与 OUTCOME 文件（遵守 AGENTS.md）。

## 风险

- op 白名单收得过紧可能拒绝历史合法用法：白名单以三层（归一化/裁剪/编译）实际支持的并集为准，实施前先跑全量测试收集现存 op 用法。
- partition→time 反推改变存量库行为：反推只在 `time is None` 时生效，存量显式声明 time 的契约不受影响；`range` 分区明确不反推。
- evolve 契约回写涉及 meta 写路径，需保证与写入事务的一致性（回写失败时写入不应部分成功）。
