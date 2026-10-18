# PLAN-081 SQLite 转 Parquet 示例结果

- 日期：2026-10-18
- 计划：[PLAN-081-sqlite-to-parquet-example.md](PLAN-081-sqlite-to-parquet-example.md)
- 状态：完成

## 实施结果

1. 新增 `examples/16_sqlite_to_parquet.py`：准备一个独立 SQLite 示例库，展示 dry-run、`Database.migrate_to()`、`verify="row-count"`，然后重开 Parquet 目标库执行查询和深度校验。
2. 抽出 `migrate_sqlite_to_parquet(source_uri, target_uri, tables=None)`，用户可以将自己的 iosql SQLite 库 URI 和空 Parquet 目标 URI 传入；可通过 `tables` 选择需要迁移的表。
3. 示例只清理自己在 `examples/runtime/` 下的两个固定目录。迁移校验要求空目标，重复运行时先清理目标目录。
4. 更新根 README、`examples/README.md`、Parquet 参考指南和 `skills/iosql-usage` 导航，并同步技能包中的示例副本。

## 静态复核与验证范围

- 已静态复核 URI 前缀、源/目标路径隔离、目标库清理范围、迁移选项以及 Parquet 重开后的查询和校验调用；与现有 `14_parquet_backend.py` 使用的迁移/校验 API 保持一致。
- `git diff --check` 通过；主示例和技能包副本通过 `cmp` 确认为相同文件。
- 本轮未运行示例或测试，因此没有运行时验证结论。
- 工作区原有未跟踪文件 `.zcodeignore` 保持未修改，也未纳入本次工作。
