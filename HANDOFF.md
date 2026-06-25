# 工作交接说明

## 当前目标

本次工作围绕 `iosql` 的 SQLite 与 PostgreSQL 互转能力展开，重点是让用户可以直接做迁移示例，不需要理解分区实现细节。

## 已完成内容

1. 增加了 SQLite -> PostgreSQL 的迁移能力。
2. 增加了 PostgreSQL -> SQLite 的回迁能力。
3. 增加了 `dry_run` 和 `verify="row-count"` 之类的迁移模式。
4. PostgreSQL 侧补齐了 `select`、批量读取、行计数等能力。
5. PostgreSQL 的表名枚举已经隐藏了分区子表，避免用户误感知内部实现。
6. 修正了 PostgreSQL calendar 分区边界的类型处理，覆盖 `text`、`date`、`timestamp`。
7. 新增了两个例子：
   - `examples/11_sqlite_to_postgresql_migration.py`
   - `examples/12_sqlite_postgresql_roundtrip.py`
8. 更新了 README、API 说明、排障文档、使用文档。
9. 更新了测试覆盖，主要集中在迁移链路、PostgreSQL 表行为、SQLite 约束、CLI 和集成示例。

## 已验证内容

已通过的验证包括：

1. 例子脚本可在没有 `POSTGRES_DSN` 时以 dry-run 方式运行。
2. `pytest` 的迁移相关测试已通过。
3. PostgreSQL 与 SQLite 的核心测试组合已通过。
4. 集成测试中除明确排除的长耗时用例外已通过。
5. `compileall` 通过。
6. `git diff --check` 通过。

## 重点文件

- `iosql/database.py`
- `iosql/engines/postgresql/table.py`
- `iosql/engines/sqlite/table.py`
- `iosql/spec.py`
- `iosql/cli.py`
- `examples/11_sqlite_to_postgresql_migration.py`
- `examples/12_sqlite_postgresql_roundtrip.py`
- `tests/test_migration.py`
- `tests/engines/postgresql/test_postgresql.py`

## 需要继续关注的点

1. 如果后续要把这组改动对外发布，建议再跑一次完整回归，尤其是有真实 `POSTGRES_DSN` 的环境。
2. 迁移实现目前以可用和清晰为优先，后续如果要扩展到更复杂的类型映射，可以再补一轮专门的兼容测试。
3. 目前仓库里已经有一批 `docs/dev/PLAN-0xx` 和对应 `OUTCOME` 文件，提交时要一并纳入，避免文档和代码脱节。

## 交接建议

接手的人可以按这个顺序继续：

1. 先看 `tests/test_migration.py`，确认迁移接口的预期行为。
2. 再看 `examples/11_sqlite_to_postgresql_migration.py` 和 `examples/12_sqlite_postgresql_roundtrip.py`，理解用户侧使用方式。
3. 最后看 `iosql/database.py` 和 `iosql/engines/postgresql/table.py`，把迁移、查询、分区隐藏逻辑串起来。

## 提交注意事项

如果现在要提交，请使用中文提交信息，并在信息里明确提到这次涉及的计划文件和结果文件，尤其是 `docs/dev/PLAN-042` 到 `docs/dev/PLAN-050` 这一组。
