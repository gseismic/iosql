# 计划索引

本索引从 2026-11-05 开始记录 `docs/plans/` 中的新计划；历史 `docs/dev/` 内容未补录。

| 更新时间（Asia/Shanghai） | 计划 | 状态 | 简要结果 |
| --- | --- | --- | --- |
| 2026-11-05 16:21 | [显式分区参数警告](PLAN-20261105-001-model-unknown-explicit-partition-warning.md) | 实施中 | 区分显式传参与默认值，检查相关分区风险。 |
| 2026-11-05 16:34 | [显式分区参数警告](PLAN-20261105-001-model-unknown-explicit-partition-warning.md) / [结果](PLAN-20261105-001-model-unknown-explicit-partition-warning-OUTCOME.md) | 已完成 | 增加显式参数被忽略时的警告，相关回归通过；登记五项既有分区问题。 |
| 2026-11-05 16:57 | [修复五项分区正确性问题](PLAN-20261105-002-model-unknown-partition-integrity.md) | 实施中 | 修复跨后端分区契约、calendar 边界和时间语义，并验证历史布局。 |
| 2026-11-05 17:11 | [迁移默认工作区切换到目标文件系统](PLAN-20261105-003-model-unknown-migration-workspace.md) | 实施中 | 调整默认工作区并验证跨盘、显式覆盖和失败恢复。 |
| 2026-11-05 17:25 | [迁移默认工作区切换到目标文件系统](PLAN-20261105-003-model-unknown-migration-workspace.md) / [结果](PLAN-20261105-003-model-unknown-migration-workspace-OUTCOME.md) | 已完成 | 本地目标默认目标盘暂存，跨盘、挂载点、远程目标回退和恢复验证通过。 |
| 2026-11-05 17:39 | [迁移命令进度与帮助示例](PLAN-20261105-004-model-unknown-migration-progress.md) | 实施中 | 增加阶段、文件与行数进度，并保留标准输出 JSON。 |
| 2026-11-05 17:48 | [迁移命令进度与帮助示例](PLAN-20261105-004-model-unknown-migration-progress.md) / [结果](PLAN-20261105-004-model-unknown-migration-progress-OUTCOME.md) | 已完成 | 阶段、文件和行数进度写入标准错误；帮助示例、JSON 输出及跨后端回归通过。 |
| 2026-11-05 18:20 | [修复五项分区正确性问题](PLAN-20261105-002-model-unknown-partition-integrity.md) / [结果](PLAN-20261105-002-model-unknown-partition-integrity-OUTCOME.md) | 已完成（WORKTREE） | 修复五项问题及复核发现的日期、时区、并发和兼容问题；全量测试通过，性能优化待评估。 |
