# PLAN-081 SQLite 转 Parquet 示例

- 日期：2026-10-18
- 状态：已完成
- 范围：新增一份可重复运行的 SQLite 引擎到 Parquet 引擎迁移示例，并更新仓库与 iosql 使用指南中的导航。

## 目标

让用户能直接看到如何从 `sqlite://` 打开 SQLite 源库，通过 `Database.migrate_to()` 将表契约和数据复制到 `parquet://` 目标库，并在目标端读取和校验结果。

## 实施步骤

1. 新增独立示例 `examples/16_sqlite_to_parquet.py`：脚本自行准备少量 SQLite 时序数据，先 dry-run，再执行带 `verify="row-count"` 的迁移，最后重开 Parquet 库查询并做深度校验。
2. 更新 `examples/README.md`、根 README 和 Parquet 参考指南，明确示例文件、运行命令和可选依赖。
3. 同步更新 `skills/iosql-usage` 中的示例文件与导航，避免仓库示例和使用技能副本不一致。
4. 静态审阅代码、清理路径、URI、契约和目标校验流程，生成结果文档；本轮不新增或运行测试。

## 约束

- 示例只删除自己在 `examples/runtime/` 下的 SQLite 源库和 Parquet 目标库。
- 使用干净目标库以满足 `verify="row-count"` 的迁移前置条件。
- 不修改迁移实现或数据格式，不提交工作区中原有的 `.zcodeignore`。
