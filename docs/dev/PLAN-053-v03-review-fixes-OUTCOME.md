# PLAN-053 结果: v0.3.0 代码 Review 修复

日期: 2026-08-22
计划: docs/dev/PLAN-053-v03-review-fixes.md
状态: COMPLETE（5/5 修复项完成，全量回归通过）

## 变更清单

### T1 类型归一化缺口（sqlite 引擎）
`iosql/engines/sqlite/table.py`
- 新增 `_SQLITE_TYPE_ALIASES` 别名表：int/bigint/smallint/tinyint/mediumint/
  boolean/bool→integer；float/real/double/numeric/decimal→float；
  text/varchar/char/string→text；blob→blob。
- `_canon_sqlite_type` 改为查表归一，截断 `(n)` 参数；无法识别返回 `None`。
- `_ensure_contract_type_compatible` 对归一结果为 `None` 的列跳过强校验
  （timestamp/json 等自定义声明回落宽松行为）。

验证：声明 `int/bigint/varchar(255)/double precision/boolean/numeric` 写入
匹配数据全部成功；str→integer 仍抛 `BadArgument`；int→float 互通不回归。

### T2 PG delete 参数化（postgresql 引擎）
`iosql/engines/postgresql/table.py`
- `delete(where)` 表名改用 `sql.Identifier`；where 经 `_where_sql`
  （Where→build_or_cases_subquery）参数化编译，支持 dict/list/Case/文本四种形式，
  与 select 同源。`None` 保持 delete_all 语义。

验证（本地无可用 PG 认证，mock 连接层验证 SQL 构造路径）：
- dict 条件 → `DELETE FROM "t2_del" WHERE ("sym" = %s)`, params=['A']
- 文本条件 → `"n" > %s`, params=[3]
- 注入串在 Case 白名单层拦截（BadArgument），不进入 SQL
- delete_all 无 WHERE、params 为空

### T3 calendar 分区 + 裸字符串 time 调和（spec.py）
`iosql/spec.py` `TableContract.__post_init__`
- time 来自裸字符串且 partition 为 duration/calendar 且列名一致时，
  unit/timezone 以分区语义推导，与 `time is None` 反推规则完全一致；
  列名不一致时不做假设。显式 TimeColumn/dict 不受影响；持久化契约经
  to_dict/from_dict 往返后已固化，重开不会二次漂移。

验证：
- `calendar(date,...,timezone=Asia/Shanghai)` + `time="date"` →
  `unit='date', tz='Asia/Shanghai'`（原为 `'ms'/UTC`）
- duration + str time 尊重分区语义；异名列保持默认值；显式对象不受影响
- 带数据写入后重开：契约往返稳定，`query(date=)` 按 date 语义命中

### T4 _schema_lock 收缩（sqlite 引擎）
`iosql/engines/sqlite/table.py` `_do_insert_part`
- NoSuchTable / NoSuchColumn 两分支的数据 insert 重试移出锁体，
  锁只覆盖 schema 演进段（meta 重读 → suggest → 补列 → 建表 → meta 回写）。
  正确性依据写入注释：重试 `ensure_table=False` 不再触发改动。

验证：`tests/test_plan052_concurrency.py` 13 例全绿（含 barrier 双线程并发加列 ×3 轮）。

### T5 类型校验采样化（sqlite 引擎）
`iosql/engines/sqlite/table.py`
- 新增 `_TYPE_CHECK_SAMPLE_ROWS = 1000`，`_ensure_contract_type_compatible`
  只扫批次头部样本。与同路径 `suggest_column_types` 的 n_suggest_check
  采样水位对齐，消除热路径 O(rows×cols) 全量扫描。

## 明确不改（复核裁定）

- **range 分区不反推 time**：PLAN-052 L47/L102 与 README 注意事项均文档化，
  属故意决策，非缺陷。
- **PG select to_dict=True 默认值**：接口决策，影响既有调用方，留待用户裁决。
- **BaseTable.insert_all 死代码下沉等结构项**：超出本轮缺陷修复范围。

## 最终验证

- 全量回归：`345 passed, 16 skipped`（与基线一致，零失败）
- 静态检查：Pyright 对比 HEAD 基线 worktree——本轮变更零新增诊断
  （存量诊断类别与基线逐一对应）

## 已知限制

- T2 的真实连接冒烟未执行（本地 PG 无密码/无 peer 角色/sudo 不可用），
  编译路径由 mock + 共用工具函数（已被 sqlite 测试覆盖）背书；
  下次有 DSN 环境时建议补跑 `tests/engines/postgresql/test_postgresql.py`。
- T5 采样意味着批次第 N(>1000) 行才首次出现的类型冲突不再由该校验拦截，
  行为回落到 SQLite affinity 层处理——与类型推断的既有采样口径一致。
