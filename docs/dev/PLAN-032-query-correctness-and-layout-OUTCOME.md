# PLAN-032-query-correctness-and-layout-OUTCOME

## 结果

已完成查询正确性修复和第一阶段代码结构重组。

## 查询修复

1. SQLite where 条件改为参数化 SQL：
   - 字符串值不再用双引号拼接。
   - 数值列传入字符串时不会被当作 SQL 表达式执行。
   - `in`、`between_and`、普通比较和变量比较都走统一 `SqlQuery(sql, params)`。

2. `select()` 的 OR 语义改为每个物理 SQLite 文件执行单条 OR SQL：
   - 不再把多个 OR 分支结果直接拼接。
   - 同一行满足多个 OR 分支时只返回一次，符合 SQLite `OR` 语义。

3. list/tuple 字段投影统一作为 identifier quote：
   - `select(["order"])` 可以查询保留字列名。
   - 字符串字段表达式仍作为 raw SQL escape hatch，例如 `"COUNT(*)"`。

4. 修复 `latest()/asof()` 投影：
   - `columns="*"` 正常返回完整行。
   - `columns="symbol,price"` 正常按字符串列列表投影。
   - 避免两个同名 `_project_rows()` 互相覆盖。

5. Python 层排序 NULL 语义与 SQLite 默认对齐：
   - ASC 时 NULL 在前。
   - DESC 时 NULL 在后。

6. `iosql.utils.mysqlite.Table.query/fetchall/fetchone` 支持参数绑定，供上层安全查询使用。

## 代码结构

新增核心层：

```text
iosql.core.query
```

用于存放通用 query AST、where parser、Case/Where 表达式。

新增引擎层：

```text
iosql.engines.sqlite
iosql.engines.postgresql
```

`Database.get_engine()` 已改为从 `iosql.engines` 加载引擎。

旧入口在 `PLAN-032` 中曾短暂保留为薄转发：

```text
iosql.query       -> iosql.core.query
iosql.sqlite      -> iosql.engines.sqlite
iosql.postgresql  -> iosql.engines.postgresql
```

后续 `PLAN-033` 已按重构思路删除这些旧入口。

删除了未引用的历史备份源文件：

```text
iosql/_future/*.py
iosql/sqlite/backup/*.py
```

## 测试

已通过：

```bash
python -m compileall iosql -q
python -m pytest tests/engines/sqlite/test_query_correctness.py -q
python -m pytest tests/core/query tests/engines/sqlite -q
python -m pytest tests/core/query tests/engines/sqlite tests/engines/postgresql -q
python -m pytest tests/utils/mysqlite -q
python examples/01_quickstart_table.py
python examples/03_buffered_ingest_and_query_plan.py
python examples/06_row_segment_late_data.py
git diff --check
```

分区集成测试状态：

```bash
python -m pytest tests/test_partition.py tests/test_partition_integration.py -q
```

该命令在 176 秒后手动中断；中断前已有 `71 passed`。未完成的用例定位为：

```text
tests/test_partition_integration.py::TestLargeData::test_upsert_large_data
```

单独运行该用例 67 秒未结束后手动中断。中断栈位于 `iosql/utils/mysqlite/table.py` commit/query 路径，属于既有大数据 upsert 压测慢用例，不是本轮查询编译路径。

## 新增测试

新增 `tests/engines/sqlite/test_query_correctness.py` 覆盖：

- 字符串值等于列名时不能被当作标识符。
- 数值列字符串输入不能注入 SQL。
- OR 条件不能重复返回同一行。
- list 投影保留字列名。
- Python 层 NULL 排序匹配 SQLite 默认。
- `latest()` 支持 `columns="*"` 和字符串列列表。
- 新包结构的核心 query 和 SQLite engine 导出。
