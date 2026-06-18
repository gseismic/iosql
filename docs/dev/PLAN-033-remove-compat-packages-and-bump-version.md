# PLAN-033-remove-compat-packages-and-bump-version

## 背景

`PLAN-032` 为降低迁移风险，暂时保留了旧包转发：

- `iosql.query`
- `iosql.sqlite`
- `iosql.postgresql`

当前用户明确要求不保持兼容性，因此这些旧入口应删除，所有代码和文档统一使用新结构。

## 目标

1. 删除旧转发包源文件。
2. 重组测试目录，使测试结构对应代码结构：
   - `tests/core/query`
   - `tests/engines/sqlite`
   - `tests/engines/postgresql`
   - `tests/utils/mysqlite`
3. 内部测试和文档统一改为：
   - `iosql.core.query`
   - `iosql.engines.sqlite`
   - `iosql.engines.postgresql`
4. 更新版本号。
5. 跑查询、SQLite、PostgreSQL 相关测试。

## 非目标

- 不恢复用户已删除的文件。
- 不重写 PostgreSQL 引擎行为。

## 验收

```bash
python -m compileall iosql -q
python -m pytest tests/core/query tests/engines/sqlite tests/engines/postgresql -q
python -m pytest tests/utils/mysqlite -q
git diff --check
```
