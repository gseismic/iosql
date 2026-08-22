# PLAN-052-user-api-unify-OUTCOME

实施依据：`docs/design/user-api-unify-20260822-consolidated.md`（合并修正稿）
计划文件：`docs/dev/PLAN-052-user-api-unify.md`
完成日期：2026-08-22
版本：0.2.5 → **0.3.0**

## 结果总览

| 任务块 | 状态 | 说明 |
|---|---|---|
| T1 table() 参数面补齐 | ✅ 完成 | time/symbol_column/session_column 下沉主入口 |
| T2 timeseries() 薄封装 | ✅ 完成 | 含 pk 别名与三类废弃参数 DeprecationWarning |
| T3 查询层接口稳定 | ✅ 完成 | op 归一化 / partition→time 反推 / select 默认值与 columns 别名 / 弃用警告 |
| T4 evolve 三语义 | ✅ 完成 | NULL 补齐（含新分区创建路径补列修复）/ 契约回写 / 类型冲突 |
| T5 错误指引 | ✅ 完成 | write() 双引擎指引 + insert_all 互斥校验 |
| T6 示例与 README 收敛 | ✅ 完成 | 14 个示例全部重写并运行通过；README 七处收敛 |
| T7 回归与发布 | ✅ 完成 | 332 passed / 16 skipped；版本 0.3.0 |

## 逐任务实施记录

### T1+T2 · database.py（commit `2307a34`，契约前置 commit `70ee697`）

- `Database.table()` 新增 `time=None, symbol_column=None, session_column=None`，纳入 spec= 互斥检查；`time` 支持字符串（spec.py 配套转 `TimeColumn`）。
- `Database.timeseries()` 重写为 `table()` 薄封装：`pk=` 别名（冲突检查）、`primary_key` 变可选；`partition_kind='calendar'` / `time_unit='date'` → 内部转 `Partition.calendar(...)` 并发 `DeprecationWarning`；`*_rows_per_file` → 映射 `Segment.rows(...)` 并发警告；duration 分支经 `PartitionSpec(column, every, kind='duration').to_partition()` 构造以支持 `"1s"` 等字符串间隔（`Partition.duration()` 工厂不转换字符串，首轮实现此处有 bug，已修）；calendar 场景 `TimeColumn.unit` 显式置 `'date'`（否则 query(date=) 裁剪失效）。
- 行为变化：timeseries() 默认不再隐式创建 segment（设计一要点 3），行数阈值统一走 `segment=`。

### T3 · 查询层（commits `c24d594`、`7b6f1af`）

- **op 归一化白名单进 Case 构造入口**（case.py）：别名表 `eq/=→==、neq→!=、gt/ge/lt/le→比较符、between/range→between_and`；非法 op 即抛 `BadArgument`（含合法列表与修复示例）；`check_field_tvalue`/`_parse_value`/`build_case_subquery` 全部裸 assert 替换。堵住"拼错 op → Case 静默通过 → 裁剪静默失效 → 延迟报错"链。
- **partition→time 反推**（spec.py）：`__post_init__` 与 `from_table_meta` 遗留分支两处一致——仅 duration/calendar 反推，range 不反推（开放问题 1 拍板不对称）。普通分区表由此直达 `query(start/end/date)` / `latest()` / `asof()`。
- **MissingTimeColumn** 错误附修复指引。
- **select 家族**（sqlite + postgresql 双引擎）：`fields="*", where=None` 默认化；`columns=` 别名（同传冲突抛 BadArgument）；共享 `resolve_select_fields` 移入 iosql/table.py。
- **弃用**：`select_all()` → 指向 `select(to_dict=True)`；`latest(group_by=)` → 指向 `by=`。

### T4 · evolve 三语义（commit `7b6f1af`）

- **语义一 NULL 补齐**：根因是 SQLite 双引号字面量陷阱——旧分区缺列时 `select "f1"` 返回字符串 `'f1'` 而非报错。修复 `_do_insert_part` NoSuchTable 分支：先算 `fresh_types`（**必须先于就地合并**，原实现因 `new_column_info = current_meta["column_info"]` 引用别名导致恒空），再合并存量列进新分区文件、把新列 ALTER 到旧分区。segment 路径经同一函数自动覆盖。调试手段：sys.settrace 行级跟踪定位到 `line 3521→3523` 直接跳转。
- **语义二 契约回写**：`_sync_contract_columns` 把动态新增列回写 `createtable_meta['table_contract']['columns']`，随 meta 持久化；重开库 `spec().columns` 不漂移。
- **语义三 类型冲突**：insert_all 入口 `_ensure_contract_type_compatible`——不兼容抛 BadArgument（含列名/固化类型/本次类型/修复建议）。放行规则：int↔float 互通、bool→integer、None 跳过、real/double≈float、**数字写入 text 声明列放行**（SQLite TEXT affinity 转换；既有测试 `test_segment_mixed_type_stats_remain_conservative` 依赖此宽松行为，设计文档"声明列冲突即现有行为报错"的说法与现状不符，以现状为准放宽并记录）。

### T5 · 错误指引（commit `7b6f1af`）

- `write()` 无契约错误改为含两条修复路径的中文指引（重新声明参数 / `spec=` 绑定），双引擎同步。
- `insert_all(replace=True, ignore=True)` 抛 BadArgument（双引擎）。

### T6a · 示例收敛（14 个文件重写）

- 全部改 `with Database(...)`；`insert_all`→`write`；dict op 统一 `==`/`between`（无 between_and/"=" 残留，grep 验收通过）；字符串示范统一 `timezone=`。
- 例 02_table 增加 `query(date=)` 演示反推能力；例 04 改 `with_partition(partition=...)` 单签名 + `segment=` 表达；例 08 手写 TableContract 改 `db.table(...)` 快捷参数（保留"高级用法仍可直接构造契约"标注）；例 09/10 删除 spec None 防御代码（信任 API 错误路径，保留目标库非空安全检查）。
- finance/03 迁移到 `Partition.calendar` 显式形式；finance/01/02/04 废弃参数迁 `segment=`；finance/02 文本 where `=`→`==`。
- 全部 14 个示例逐一运行通过（11/12 未改动）。

### T6b · README 收敛

- 特性区/快速开始（with + write + 文本 where + select 免参）/写入分层说明/查询小节（select 与 query 返回语义差异、合法 op 对照表）/timeseries 薄封装与废弃迁移表/calendar 示例 query(date=)/session 示例 pk/latest by= 唯一拼写/doctor 输出 timezone=/新增"动态扩列语义"与"导出面分层"两节/变更记录 v0.3.0。

### T7 · 发布

- 版本 0.2.5 → 0.3.0；全量 pytest **332 passed, 16 skipped**（基线 308 + 新增 24）；68 warnings 全部来自测试显式调用弃用 API（预期）。

## 测试覆盖对照（设计文档第五节 1-11）

| # | 设计条目 | 测试 |
|---|---|---|
| 1 | timeseries pk ≡ primary_key | `test_timeseries_pk_alias_equivalent_to_primary_key` |
| 2 | timeseries ≡ 等价 table() 契约 | `test_timeseries_equivalent_to_table_contract`（spec dict 完全相等） |
| 3 | dict between ≡ between_and | `test_dict_between_normalized_like_between_and` |
| 4 | 非法 op 即时 BadArgument | `test_illegal_op_raises_bad_argument_at_case_construction` + 别名归一化 + 值校验共 3 个 |
| 5 | tz= ≡ timezone= 解析 | `test_string_partition_tz_and_timezone_parse_equal` + 规范输出测试 |
| 6 | mode 回落 conflict；互斥 | `test_write_mode_falls_back_to_contract_conflict` + `test_insert_all_replace_and_ignore_mutually_exclusive` |
| 7 | calendar 反推 query(date=)；range 不反推 | `test_calendar_partition_infers_time_enables_date_query` + `test_range_partition_does_not_infer_time` |
| 8 | write ≡ insert_all | `test_write_delegates_to_insert_all_with_same_result` |
| 9 | evolve 三语义 | NULL 补齐 / 回写重开 / 类型冲突 / 数值互通 / text 宽松 共 5 个 |
| 10 | columns= 别名 | `test_select_columns_alias_equivalent_to_fields`（select/iter_select/explain_select + 冲突） |
| 11 | 弃用警告且等价 | partition_kind/time_unit/rows_per_file/group_by/select_all 共 3 个 |

另附第八节验收片段：table(time=, session_column=) 直达查询、write 无契约错误指引。

既有测试更新（行为变化的固化）：text_where 断言 `'='→'=='`（归一化后规范形式）、write 无契约消息断言、calendar 无 time 契约断言改为验证反推结果、timeseries reopen 的 `partition.every` 断言 `'1day'→86_400_000`（RuntimePartition meta 往返的等价表示）、doctor/partition_string 输出 `tz=→timezone=` 共 9 处。

## 遗留项

1. `WriteResult.rows_seen → rows_received` 更名：按开放问题 3 留待 v0.3 统一处理，本轮未动。
2. `open_table()` 拆入口（开放问题 4）：轻方案已落地（错误指引），重方案留 doctor/autotune 阶段评估。
3. `Case` 运算符重载移除（开放问题 5）：本轮文档不示范、导出保留，v0.4 评估。
4. `skills/iosql-usage/` 随包分发的技能文档仍含旧示范（tz=、between_and、primary_key 等）：计划范围外未同步，建议下一轮随 skill 更新一并处理。
5. PostgreSQL 引擎未实现 evolve 三语义与类型冲突检查（计划非目标：SQLite 后端优先）。
6. timeseries() 默认不再隐式建 segment 属有意行为变化，已在 README 记录；存量库不受影响（契约持久化于 meta）。

## Review 轮（2026-08-22，commit `e8d8385`）

全量代码 review + 多线程场景测试后加固：

- **并发演进竞争修复**：并发线程同时给不同新分区写各自新列时，各自基于过时 `current_meta` 快照建分区文件——互相缺列且 meta 后写覆盖丢列。新增 `_schema_lock` 串行化 `_do_insert_part` 的 NoSuchTable/NoSuchColumn 冷路径，并在锁内**重读** current_meta（入口快照在等锁期间可能已被刷新）。稳态插入不经过异常分支，热路径零锁开销。
- **时区一致性**：`timeseries()` 显式传 calendar 分区时，TimeColumn 时区优先取分区自身 tz。
- **新增 `tests/test_plan052_concurrency.py` 13 例**：不同/相同分区并发写、barrier 同步双线程并发加不同新列（3 轮 ×10 次复跑稳定）、同分区分支并发加列、读并发于演进 ALTER、首次分区创建竞争、`max_open_partitions=2` 下 LRU 驱逐并发写、latest 并发只读、独立 write_buffer、Case 归一化线程安全、iter_select limit+columns 组合、空表 select、calendar 分区 tz 一致性。
- 全量回归 **345 passed, 16 skipped**。
- **A/B 回归守护实证**：将 `_schema_lock` 猴补丁为空锁模拟修复前行为，演进竞争脚本**首轮即复现**缺列（KeyError）；恢复真锁后 **20/20 轮零失败**——证明并发测试对竞争具备真实守护力而非恒绿摆设。死锁审计：`_schema_lock` 仅在这两个异常分支获取；持 `_db_parts_condition` 的路径从不反向等待 `_schema_lock`，无环。
- **已知限制（既有，非本轮引入）**：meta 整体覆盖模式在其他声明类路径（如 `partition.setter`、运行中 `use_contract` 与并发写交叉、`force_partition` 覆盖写 table.py `_update_meta` 调用点）仍存在理论上的 lost-update 窗口；这些是用户驱动的低频声明操作，风险评级低，未纳入本轮范围，留待 meta 存储改造时统一处理。
- **独立复核（2026-08-22）**：由独立子代理按怀疑标准执行全部验证步骤——复跑定向测试（37 passed）、源码级确认两分支加锁与锁内重读、独立编写竞争测试装置复现 A/B 结论（无锁 20/20 失败 / 真锁 20/20 通过）、自证全量 345 passed/16 skipped、三锁死锁审计无环、遗留项逐条判定为范围决策而非缺陷。裁定：**COMPLETE**。

## 提交记录

| commit | 内容 |
|---|---|
| `70ee697` | 契约层 time 字符串化 + duration/calendar 分区反推 + timezone= 规范输出（T3b） |
| `2307a34` | table() 参数补齐 + timeseries() 薄语法糖（T1/T2） |
| `c24d594` | Case op 归一化白名单堵静默失效链（T3a） |
| `7b6f1af` | select 家族默认值/columns 别名/弃用 + evolve 三语义 + 写入错误指引（T3c/T4/T5） |
| `e07fe58` | docs: 设计文档与 PLAN-052 入库 |
| `0fadb5f` | 回归修复 + tests/test_user_api_unify.py（24 例）+ 测试断言更新 |
| `6c6cbc6` | 示例全面收敛（T6a，14 个文件） |
| `de4e72b` | README 收敛 + 版本 0.3.0 + 本 OUTCOME（T6b/T7） |
| `e8d8385` | review 轮：并发演进竞争修复（_schema_lock）+ 多线程场景测试 13 例 |
