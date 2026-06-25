# PLAN-048 PostgreSQL 迁移真实集成验证与文档化示例

## 背景

`PLAN-047` 已补齐 SQLite 到 PostgreSQL 迁移的 dry-run、row-count 校验和 PostgreSQL 目标侧基础查询。当前还缺少两个面向用户落地的证据：

1. 在真实 PostgreSQL 服务可用时，测试应覆盖完整的 SQLite 源库迁移、校验和查询。
2. 用户需要一个可直接运行的示例脚本，串起建 SQLite 源库、dry-run、迁移、校验和目标侧查询。

## 目标

1. 在 `POSTGRES_DSN` 可用时增加真实集成测试：
   - SQLite 源库写入；
   - `to_postgresql(..., dry_run=True)`；
   - `to_postgresql(..., verify="row-count")`；
   - 目标 PostgreSQL `select()` / `count_rows()`；
   - `Database("postgresql://...").table_names()` 不暴露 native partition 子表。
2. 增加 mock 测试覆盖 PostgreSQL `table_names()` 过滤 partition child 的 SQL 形态。
3. 新增示例 `examples/11_sqlite_to_postgresql_migration.py`：
   - 默认构造本地 SQLite demo 源库；
   - 未设置 `POSTGRES_DSN` 时只打印如何运行，不连接 PostgreSQL；
   - 设置 `POSTGRES_DSN` 后执行 dry-run、迁移、row-count verify 和目标查询；
   - 示例中用户只操作逻辑表，不接触 partition 子表。
4. 更新 README、examples README、skill 文档，增加生产迁移 checklist 和 calendar/date 分区边界说明。

## 非目标

1. 不要求本地必须启动 PostgreSQL；无 `POSTGRES_DSN` 时真实集成测试继续 skip。
2. 不实现 checkpoint/resume。
3. 不重写 PostgreSQL native partition 的 calendar/date 边界策略。
4. 不新增第三方依赖。

## 实施步骤

1. 更新 `tests/engines/postgresql/test_postgresql.py`，增加真实迁移集成测试。
2. 在 `tests/test_migration.py` 增加 `Database.table_names()` child partition 过滤 mock 测试。
3. 新增 `examples/11_sqlite_to_postgresql_migration.py`。
4. 更新 `README.md` 示例列表和迁移 checklist。
5. 更新 `examples/README.md`、`skills/iosql-usage/SKILL.md`、`usage-patterns.md`。
6. 运行相关测试、示例 smoke、`git diff --check`。
7. Review 后生成 OUTCOME。

## 验收标准

1. 无 `POSTGRES_DSN` 时测试 suite 正常 skip 真实 PostgreSQL 测试。
2. 有 `POSTGRES_DSN` 时能真实迁移 SQLite demo 表到 PostgreSQL，并通过 `verify="row-count"`。
3. 示例脚本无 `POSTGRES_DSN` 时不失败；有 `POSTGRES_DSN` 时执行完整迁移流程。
4. README 和 skill 明确推荐 `dry_run` + `verify="row-count"` 的迁移流程。
