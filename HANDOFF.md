# HANDOFF — PLAN-052 用户接口统一与稳定（已完成）

> **终态说明（2026-08-22）**：PLAN-052 全部任务块（T1~T7）已完成、测试通过并提交。
> 权威结果记录见 `docs/dev/PLAN-052-user-api-unify-OUTCOME.md`；本文保留为过程档案，
> 第 4 节的 bug 排查记录对后续类似问题仍有参考价值。若你接手的是新任务，请直接读
> OUTCOME 与设计文档，忽略本文的"剩余工作"清单（均已完成）。

---

## 1. 任务是什么

用户要求：根据 `docs/design/user-api-unify-20260822-consolidated.md` 优化代码。

- **设计文档（实施依据，必读）**：`docs/design/user-api-unify-20260822-consolidated.md`
  —— 核心是第三节"接口设计建议"、第五节"测试设计 1-11"、第八节"期望的用户接口"（**可执行验收基准**）。
- **实施计划**：`docs/dev/PLAN-052-user-api-unify.md` —— 任务块 T1~T7，含依赖顺序。
- 按 AGENTS.md 规范：实施完成后生成 `docs/dev/PLAN-052-user-api-unify-OUTCOME.md`，按任务块拆中文 commit（commit 消息记录 PLAN 与 OUTCOME 文件），docs/ 目录要提交。

**范围拍板（设计文档第七节）**：只做 SQLite 后端接口稳定；新词汇后端中立；不含 auto-partition；不移除任何公开符号（全部走兼容别名 + DeprecationWarning）。

## 2. 当前系统状态

- 版本：`iosql/version.py` `__version__ = '0.2.5'`（T7 需 bump 到 0.3.0）
- **测试基线（改动前）**：`308 passed, 16 skipped`（`python -m pytest tests/ -q`，约 2 分钟）
- git 工作区：已修改 7 个源码文件（见第 3 节），未跟踪 4 个 docs 文件（3 个设计文档 + PLAN-052）
- Python 3.12 环境，仓库根目录 `/home/lsl/macbook/pai-studio-fin/library/iosql`
- 测试命令：`python -m pytest tests/ -q`；单点冒烟用 heredoc python 脚本

## 3. 已完成的修改（T1/T2/T3/T5 全部 + T4 部分）

### 3.1 `iosql/database.py`（T1+T2 完成）

- `Database.table()` 新增 `time=None, symbol_column=None, session_column=None` 三个参数，透传进 `TableContract`（参与 spec= 互斥检查；`time` 支持传字符串，自动转 `TimeColumn`）。
- `Database.timeseries()` **完全重写**为 `table()` 薄封装：
  - 新签名：`timeseries(table_name, time, pk=None, primary_key=None, columns=None, *, partition_every='1day', timezone='UTC', symbol_column='symbol', session_column=None, partition=None, segment=None, order_key=None, conflict='append', schema_policy='evolve', force_partition=False, partition_kind=None, time_unit=None, target_rows_per_file=None, min_rows_per_file=None, max_rows_per_file=None)`
  - `primary_key` 从必填变可选（补了 `pk` 别名 + 冲突检查）。
  - 废弃参数触发 `DeprecationWarning` 且行为等价：`partition_kind='calendar'`→内部转 `Partition.calendar(...)`；`time_unit='date'`→同样转 calendar；`*_rows_per_file`→映射为 `Segment.rows(...)` 传 `segment=`。
  - **duration 分支不透传 timezone**（duration 分区按 UTC 对齐是文档化行为，设计文档明确不得虚构该参数）。
  - `order_key` 默认 `time`。
- imports：新增 `warnings`、`Partition`、`Segment`；移除了不再使用的 `TableSpec`、`CalendarPartition`。

### 3.2 `iosql/spec.py`（T3b 完成）

- `TableContract.__post_init__`：
  - 新增 `time` 为 str 时自动转 `TimeColumn`。
  - **partition→time 反推**：`time is None` 且 partition.kind ∈ {duration, calendar} 时，从 `partition.column` 反推 `TimeColumn`（calendar→unit='date'+timezone；duration→unit='ms'+UTC）。**range 明确不反推**（设计开放问题 1 拍板的不对称）。
- `TableContract.from_table_meta` 的遗留 meta 分支同步了同样的反推规则（原来对 range 也反推，已改为不反推）。
- `format_partition_spec`：calendar 输出从 `tz=` 改为 `timezone=`（解析层 `tz=` 仍兼容，`_parse_partition_expr_options` 里的 tz→timezone 归一化未动）。

### 3.3 `iosql/core/query/case.py`（T3a 完成）

- 模块级新增：
  - `_OP_ALIASES`：`eq/=→==`、`neq→!=`、`gt→>`、`ge→>=`、`lt→<`、`le→<=`、`between/range→between_and`
  - `_CANONICAL_OPS = ('==','!=','>','>=','<','<=','between_and','in')`
  - `normalize_case_op(op)`：归一化 + 白名单校验，非法 op 抛 `BadArgument`，信息含合法 op 列表（展示用户语言 `between`）与修复示例。
- `Case.__init__` 入口调用归一化（**堵住静默失效链**：拼错 op 原来会静默通过构造、分区裁剪失效、延迟到 SQL 编译才报错）。
- `check_field_tvalue` 与 `_parse_value` 的全部裸 `assert` 替换为带指引的 `BadArgument`。
- 内部唯一 Case 构造点 `table.py` `_pk_cases` 用 `'='`，在别名表内，安全。

### 3.4 `iosql/engines/sqlite/query_util.py`（T3a 完成）

- `build_case_subquery` 的 assert 替换为 BadArgument；`Unsupported op` 错误补合法 op 列表与示例（防御层，正常路径经 Case 归一化后不可达）。

### 3.5 `iosql/engines/sqlite/table.py`（T3c/T5 完成，T4 部分）

- 模块级新增 `_canon_sqlite_type`（real/double→float 归一）。
- `select / iter_select / iter_select_batches / explain_select`：签名默认化 `fields='*', where=None`；新增 `columns=` 别名（与 `fields` 同传且 fields 非 '*' 时抛 BadArgument）；共用 `resolve_select_fields`（从 `iosql/table.py` 导入）。
- `select_all`：触发 DeprecationWarning（指向 `select(to_dict=True)`），行为不变。
- `latest`/`asof` 的 `group_by=`：触发 DeprecationWarning（指向 `by=`），冲突检查保留。
- `_query_time_column`：`MissingTimeColumn` 错误信息附修复指引（传 time= 或用 duration/calendar 分区自动反推）。
- `write()` 无契约错误：附两条修复路径（重新声明参数 / `spec=` 绑定）。
- `insert_all`：`replace=True, ignore=True` 同传抛 BadArgument（互斥）。
- **T4 evolve 语义三**：`_ensure_contract_type_compatible(contract, rows)` 在 `insert_all` 入口调用——契约已声明类型的列，数据类型不兼容时抛 BadArgument（含列名、契约类型、数据类型、修复建议）。int/float 互通放行，bool 按 integer，None 跳过。
- **T4 evolve 语义二**：`_sync_contract_columns(createtable_meta)` 把动态新增列回写进 `createtable_meta['table_contract']['columns']`，随 `_update_meta` 持久化。已在 `_do_insert_part` 的 NoSuchTable 分支和 NoSuchColumn 分支接线。
- **T4 evolve 语义一（进行中，有 bug，见第 4 节）**：NoSuchTable 分支（新分区文件创建路径）加了"合并存量列 + 对旧分区补新列"的逻辑，**但 fresh_types 恒为空，bug 未修**。

### 3.6 `iosql/engines/postgresql/table.py`（同步完成）

- `select / iter_select / iter_select_batches` 同样默认化 + `columns=` 别名；`select_all` 弃用警告；`write()` 无契约指引；`insert_all` 互斥校验。imports 加 `warnings`，`BaseTable` 导入处加 `resolve_select_fields`。
- 注意 select 内部局部变量从 `columns` 改名为 `columns_` 避免与参数遮蔽。

### 3.7 `iosql/table.py`（BaseTable 模块）

- 新增共享函数 `resolve_select_fields(fields, columns, func_name)`（两个引擎共用）。

## 4. ✅ 已修复的 BUG（evolve 语义一，保留记录供参考）

> **状态更新（2026-08-22 提交前）：已修复并验证通过**——`fresh_types` 计算挪到合并循环之前，且 `data_suggested_types` 的值是类型字符串（直接用 `type_`，不是 `item["type"]`）。冒烟全绿：NULL 补齐、契约回写、重开持久化、类型冲突、数值互通/bool/NULL 不误报、segment 路径补列。以下为原始排查记录。

### 现象

evolve 语义一冒烟测试失败：`KeyError: 'f1'`。场景：

```python
factors = db.table("factors", pk=["symbol","ts"], columns={"symbol":"text","ts":"integer"},
                   partition=Partition.duration("ts", every="1day"))
factors.write([{"symbol":"AAPL","ts":1735689600000}])                     # 分区1: 只有 symbol/ts
factors.write([{"symbol":"AAPL","ts":1735776000000,"f1":0.3,"f2":0.7}])  # 分区2: 新列
factors.select(columns=["symbol","ts","f1"], to_dict=True, order_by="ts")
# 期望: 分区1的 f1 为 None；实际: 旧行返回字符串 'f1'（SQLite 双引号字面量陷阱）
```

根因链：分区 1 的 SQLite 文件没有 f1 列 → `select "f1" from ...` 在 SQLite 中双引号标识符解析失败后**回退为字符串字面量** → 返回 `'f1'` 字符串而非报错。语义二的契约回写、meta 合并都已工作正常（已验证：meta columns = ['symbol','ts','f1','f2']，新分区文件有全部列），缺的是**把新列 ALTER 进旧分区文件**。

### 已定位的根因（sys.settrace 确认，勿重复排查）

`_do_insert_part` 的 `except NoSuchTable:` 分支（`iosql/engines/sqlite/table.py` 约 3495-3542 行）：

```python
if current_meta:
    new_column_info = current_meta["column_info"]        # ← 这是引用，不是拷贝！
    for col, item in createtable_meta["column_info"].items():
        if col not in new_column_info:
            new_column_info[col] = item                  # ← 就地把 f1/f2 合并进了 current_meta["column_info"]
    createtable_meta["column_info"] = new_column_info
    fresh_types = {
        col: item["type"]
        for col, item in data_suggested_types.items()
        if col not in current_meta["column_info"]        # ← 此时 f1/f2 已在里面 → 恒空
    }
    if fresh_types:                                      # trace 证实: 恒 False，从未进入
        self._add_columns_for_all_partitions(fresh_types)
```

trace 输出显示 `line 3521 → line 3523` 直接跳转（跳过 3522 的 `_add_columns_for_all_partitions` 调用），证实 `fresh_types` 恒为空。

### 精确修复方案

把 `fresh_types` 的计算挪到合并循环**之前**：

```python
if current_meta:
    fresh_types = {
        col: item["type"]
        for col, item in data_suggested_types.items()
        if col not in current_meta["column_info"]
    }
    for col, item in createtable_meta["column_info"].items():
        if col not in current_meta["column_info"]:
            current_meta["column_info"][col] = item
    createtable_meta["column_info"] = current_meta["column_info"]
    if fresh_types:
        self._add_columns_for_all_partitions(fresh_types)
```

`_add_columns_for_all_partitions` 内部对 `DuplicateColumn` 是 pass（幂等），安全。修好后注意该分支尾部已有的 `_sync_contract_columns(createtable_meta)` 调用顺序不要破坏。

### 修复后的验证脚本（直接可跑）

```bash
python - <<'EOF'
import tempfile
from iosql import Database, Partition
from iosql.exception import BadArgument
tmp = tempfile.mkdtemp()
with Database(f"sqlite://{tmp}/ev.iosql") as db:
    factors = db.table("factors", pk=["symbol","ts"], columns={"symbol":"text","ts":"integer"},
                       partition=Partition.duration("ts", every="1day"))
    factors.write([{"symbol":"AAPL","ts":1735689600000}])
    factors.write([{"symbol":"AAPL","ts":1735776000000,"f1":0.3,"f2":0.7}])
    rows = factors.select(columns=["symbol","ts","f1"], to_dict=True, order_by="ts")
    assert rows[0]["f1"] is None and rows[1]["f1"] == 0.3, rows
    assert "f1" in factors.spec().columns
with Database(f"sqlite://{tmp}/ev.iosql") as db:
    assert "f1" in db.table("factors").spec().columns   # 重开仍在
    print("evolve 语义一+二 OK")

# 语义三：类型冲突
with Database(f"sqlite://{tmp}/ev2.iosql") as db:
    t = db.table("t", pk=["id"], columns={"id":"integer"}, partition=Partition.range("id", every=100))
    t.write([{"id":1,"x":1}])
    try:
        t.write([{"id":2,"x":"abc"}]); raise SystemExit("FAIL")
    except BadArgument as e:
        assert "x" in str(e) and "integer" in str(e) and "text" in str(e), e
    print("evolve 语义三 OK")
EOF
```

已验证通过的其他冒烟项（修复语义一后应全绿）：T1 time/session_column+query、T3b calendar 反推+query(date=)、range 不反推+MissingTimeColumn 指引、T2 timeseries≡table 契约一致、T2 三类 DeprecationWarning、T3a op 归一化与 BadArgument 指引、T3c select() 免参/columns 别名/文本与 dict where、`tz=`≡`timezone=` 解析等价 + `to_string()` 输出 `timezone=`、write() 无契约指引、insert_all 互斥、select_all/group_by 弃用警告、evolve 数值互通/bool/NULL 不误报。

## 5. 剩余工作（按顺序）

1. **修第 4 节的 bug**，跑通验证脚本。
2. **跑全量测试** `python -m pytest tests/ -q`，修复因行为变化破坏的既有测试。**已知预期破坏点**：
   - `tests/test_partition_string.py:45`：断言 `to_string()` 输出 `tz=Asia/Shanghai` → 现在输出 `timezone=`，需更新断言（第 38 行 parse 断言不用动，解析兼容不变）。
   - `tests/test_cli.py:949-950`：doctor 建议输出含 `tz=Asia/Shanghai` → 现在会是 `timezone=`，需更新。
   - 其他 grep 到 `tz=` 的测试（`test_migration.py:442/463/484` 字符串解析、`test_partition.py` 的 `CalendarPartition(tz=...)` 对象构造）**不受影响**：前者解析层兼容，后者内部类参数未改。
   - 关注 `db.timeseries()` 签名变化对 `tests/engines/sqlite/test_timeseries_contract.py` 的影响：旧调用 `db.timeseries(name, time, primary_key=...)` 仍兼容；`TableContract.timeseries(...)` 类方法**没有改**，仍可用；但 `db.timeseries(..., target_rows_per_file=...)` 现在会发 DeprecationWarning（一般不 fail，除非 -W error）。逐个看失败信息修。
   - `op` 白名单收紧：如果既有测试用了白名单外的 op 会抛 BadArgument（已 grep 测试源码未发现，但以实际跑为准）。
   - partition→time 反推改变了 `spec()` 输出：某些断言 `spec.time is None` 的测试可能需要更新。
3. **新增 `tests/test_user_api_unify.py`**，覆盖设计文档第五节测试 1-11（用 `pytest.warns(DeprecationWarning)` 断言弃用路径）。本会话的冒烟脚本可直接改写成测试。
4. **T6a 示例收敛**（对照设计文档第八节"场景覆盖对照表"逐行）：
   - 全部 examples/（不含 mysqlite/）：`insert_all`→`write`；裸 `db.close()`→`with Database(...)`；dict where 的 `"op": "between_and"`→`"between"`、`"op": "="`→`"=="`；字符串 partition 示范 `timezone=`；
   - 例 08：手写 `TableContract(TimeColumn(...), PhysicalLayout(...))` 改为 `db.table(...)` 快捷参数（T1 已支持 time=/segment=），加注释"高级用法仍可直接构造契约"；
   - 例 09/10：删掉 `if spec is None: raise RuntimeError(...)` 手动防御代码，直接调用（信任 API 错误指引路径）；
   - 例 04：`with_partition(every=...)` 改示范 `with_partition(partition=...)`。
   - 验收：逐个运行 `python examples/XX.py`（11/12 无 POSTGRES_DSN 走 dry-run）；grep 确认无 `between_and`、`op": "="`、`tz=` 示范、quickstart 无裸 `db.close()`。
5. **T6b README 收敛**：
   - 合法 op 表（用户语言 `== != > >= < <= between in`）；
   - timeseries 薄封装说明 + 废弃参数迁移表（partition_kind/time_unit/*_rows_per_file → 新写法）；
   - "动态扩列语义"小节（三语义：旧分区 NULL 补齐 / 契约回写 / 类型冲突报错）；
   - 导出面两层示范（用户层：Database/Partition/Segment/Where+结果类型；高级层：TableContract/TableSpec/PhysicalLayout/TimeColumn）；
   - `query()` 惰性 fluent 对象 vs `select()` 急切列表的差异标注；
   - `latest(by=)` 唯一拼写；`select()` 参数可省略示例；README 内 `tz=Asia/Shanghai` 示范统一为 `timezone=`。
   - `skills/iosql-usage/` 随包分发的技能文档也有旧示范（tz=/between_and），计划未列，**不改**（时间充裕可同步，非必须）。
6. **T7 发布**：`iosql/version.py` bump `'0.3.0'`；README 变更记录加 v0.3.0 条目；全量 pytest + 全部示例运行；写 `docs/dev/PLAN-052-user-api-unify-OUTCOME.md`（逐任务记录结果、测试数字、遗留项）。
7. **提交**：按任务块拆中文 commit。建议拆分：①T1+T2（database.py）②T3（case.py/query_util.py/spec.py/两引擎 table.py 查询面）③T4（evolve）④T5+新测试 ⑤T6 示例+README ⑥T7 版本+OUTCOME。commit 消息记录 PLAN-052 与 OUTCOME 文件名（遵守 AGENTS.md）。未跟踪的 3 个设计文档 + PLAN-052 一并提交。

## 6. 关键设计约束（不要踩坑）

- **duration 分区无时区概念**：`Partition.duration()` 没有 timezone 参数，`timeseries()` 只有 calendar 分支透传 timezone。别"补齐"这个参数。
- **range 分区不反推 time**：设计文档开放问题 1 拍板的不对称，`spec.py` 的 `__post_init__` 和 `from_table_meta` 两处都已实现，改动时保持一致。
- **op 白名单**：canonical 集合 `('==','!=','>','>=','<','<=','between_and','in')`。展示给用户用 `between`（用户语言），内部规范形式 `between_and` 不出现在示例。
- **类型冲突检查的放行规则**：int↔float 互通、bool→integer、None 跳过、real/double≈float（`_canon_sqlite_type`）。收紧前先跑全量测试。
- **不改 `iosql.utils.mysqlite` 内部接口**（计划非目标）。
- **不改 `TableContract.timeseries` 类方法**（保留旧签名，`db.timeseries()` 才是新薄封装）——存量测试在用。
- PostgreSQL 引擎：`table()` 新参数只是存进契约，不实现 SQLite 特有行为（计划非目标）。
- 所有弃用路径必须真的发 `DeprecationWarning`（不能静默），测试用 `pytest.warns` 固化。
- 本仓库有注释审查 hook：设计约束类注释（如"range 不反推"、"SQLite 双引号字面量陷阱"）和公开 API docstring 允许且必要；解释性废话注释会被要求删改。

## 7. 快速上下文索引

| 主题 | 位置 |
|---|---|
| 设计文档（验收基准第八节） | `docs/design/user-api-unify-20260822-consolidated.md` |
| 实施计划 T1-T7 | `docs/dev/PLAN-052-user-api-unify.md` |
| table()/timeseries() | `iosql/database.py` |
| 契约/partition 反推/序列化 | `iosql/spec.py`（`TableContract.__post_init__`、`from_table_meta`、`format_partition_spec`） |
| op 归一化 | `iosql/core/query/case.py`（`normalize_case_op`） |
| select 家族/evolve/错误指引 | `iosql/engines/sqlite/table.py`（`select` 至 `_do_insert_part`） |
| **bug 所在** | `iosql/engines/sqlite/table.py` `_do_insert_part` 的 `except NoSuchTable:` 分支（约 3495-3542 行） |
| 字段别名共享函数 | `iosql/table.py` `resolve_select_fields` |
| PG 引擎同步 | `iosql/engines/postgresql/table.py` |
| examples 待收敛清单 | 设计文档第八节末尾"场景覆盖对照表" |
