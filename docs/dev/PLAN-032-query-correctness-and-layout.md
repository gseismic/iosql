# PLAN-032-query-correctness-and-layout

## 背景

本轮审查发现 SQLite 查询链路仍有几类不符合“接近原生 SQLite + 自动分区”目标的问题：

- 字符串值使用双引号拼 SQL，可能被 SQLite 解析为标识符，导致错误结果。
- 数值列按列类型决定不加引号，用户传入字符串时可能形成 SQL 注入或错误表达式。
- OR 条件拆成多次查询后直接拼接，可能返回重复行。
- `latest()` 的列投影函数被同名方法覆盖，`columns="*"` 和字符串列列表会失败。
- Python 层 `order_by` 的 NULL 排序与 SQLite 默认语义不一致。
- list/tuple 字段投影没有 quote identifier，保留字列名会查询失败。

同时当前包结构中 `iosql.sqlite`、`iosql.postgresql` 与通用 `iosql.query` 平行，边界不清晰。查询 AST/Where 解析应属于核心层，引擎实现应属于 engines 层。

## 目标

1. 修复 SQLite 查询 SQL 生成和结果合并语义，优先保证结果正确。
2. 保持 `select()` 的 SQLite 心智：支持未投影字段排序、保留字列名 list 投影、合理 NULL 排序。
3. 修复 `latest()/asof()` 的 `columns="*"` 和字符串列列表投影。
4. 补充覆盖上述问题的回归测试。
5. 重组代码目录：
   - 新增 `iosql/core/query` 存放通用 query AST、where parser 和 case 工具。
   - 新增 `iosql/engines/sqlite`、`iosql/engines/postgresql` 存放具体引擎实现。
   - 原 `iosql.query`、`iosql.sqlite`、`iosql.postgresql` 后续按重构要求删除。

## 非目标

- 不在本轮完整重写 PostgreSQL 查询实现。
- 不实现全局主键索引、tombstone 或 compaction。
- 不把整个 `Table` 大文件拆分为多个类；本轮只做包层次和查询编译边界整理。

## 实施步骤

1. 建立新包结构并迁移/转发通用 query 与引擎入口。
2. 重写 SQLite case SQL builder，使 value 生成安全、字段投影可 quote，并支持参数绑定形态。
3. 调整 SQLite `Table.select()` 对 OR 结果去重、排序、limit 和投影的处理。
4. 修复 `latest()` 投影函数命名冲突。
5. 补充测试覆盖字符串字面量、数值列恶意字符串、OR 去重、保留字字段、NULL 排序和 latest 投影。
6. 运行核心测试和示例，生成 OUTCOME 文档并提交。

## 验收

```bash
python -m compileall iosql -q
python -m pytest tests/core/query tests/engines/sqlite tests/engines/postgresql -q
python -m pytest tests/utils/mysqlite -q
```
