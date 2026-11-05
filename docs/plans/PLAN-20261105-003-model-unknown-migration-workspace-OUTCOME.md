# 迁移默认工作区调整结果

- 创建时间：2026-11-05 17:25 Asia/Shanghai
- 模型：环境仅提供 GPT-6 系列信息，实际模型 ID 未提供（`model-unknown`）。
- 结果版本：`WORKTREE`，基准提交 `e9dee736a579f2319af91b2f022f1b0706968dcd`。本次涉及 `iosql/migration.py`、`iosql/cli.py`、`tests/test_safe_migration.py`、`README.md`、`docs/design/migration-api-20261018-unified.md` 和 `skills/iosql-usage/references/` 下的迁移文档，以及本计划与索引；工作区还存在其他任务的未提交改动，结果尚未固定为提交。
- 关联计划：`docs/plans/PLAN-20261105-003-model-unknown-migration-workspace.md`。

## 完成情况

- 本地 SQLite/Parquet 目标默认在目标文件系统创建独立迁移工作区，通常位于目标父目录的 `.iosql-locks/<目标标识>.workspaces/<任务ID>/`；目标根自身是独立挂载点时使用目标内保留的 `__iosql_migrations__/`。同文件系统的最终发布仍使用重命名。
- 本地目标先完成身份和契约预检，再创建工作区；保留显式 `temp_dir`、目标内部路径拒绝规则、PostgreSQL 目标既有回退策略、失败暂存和成功清理行为。预览不创建目标工作区。
- 更新 CLI 帮助、README 中用户的跨盘迁移命令、项目 skill 指南和历史设计文档的变更提示。未执行用户的真实数据库迁移。
- 自查了预检顺序、已有目标合并、同库复制到新表、崩溃恢复和目录清理；未发现需要额外延期的问题。计划范围未发生实质变化。

## 验证

- 迁移、另存、快照、CLI、PostgreSQL 模拟及恢复测试：`238 passed, 4 skipped`；另外同库复制到新表的 SQLite/Parquet 测试 `2 passed`。
- 真实 PostgreSQL 测试：SQLite → PostgreSQL → Parquet 往返及 Database/Table 另存共 `3 passed`。原有测试实例起初未运行，启动项目自带的本机独立测试实例后重跑通过。
- Parquet 后端测试 `34 passed`；SQLite 重分区相关测试 `2 passed`。常规测试使用系统 Python，因为项目 `.venv` 未安装 pytest。
- 在 `/dev/shm` 与临时目录之间验证实际跨文件系统迁移，确认默认工作区位于目标侧、成功发布不调用跨盘 payload 复制；挂载点处理通过模拟设备 ID 和完整迁移测试验证。
- 在用户选择的 Z1 文件系统上使用独立临时库执行 CLI 预览和迁移，验证默认工作区、行数、全部样本内容和工作区清理；真实目标目录未创建。README 示例通过 `bash -n`，`git diff --check` 通过。

## 遗留与提交状态

暂无本任务遗留问题；真实 61 GB 数据需由用户自行迁移，未进行大数据量性能测试。更改未提交、未推送，工作区中其他任务的改动保留。
