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
| 2026-11-05 18:21 | [SQLite 到 Parquet 迁移性能优化](PLAN-20261105-005-model-unknown-sqlite-parquet-migration-performance.md) | 实施中 | 优化源表计数与复制、列式导入及大量分区下的开销，保留校验与发布语义。 |
| 2026-11-05 18:44 | [SQLite 到 Parquet 迁移性能优化](PLAN-20261105-005-model-unknown-sqlite-parquet-migration-performance.md) / [结果](PLAN-20261105-005-model-unknown-sqlite-parquet-migration-performance-OUTCOME.md) | 已完成（WORKTREE） | 优化逐文件统计与复制、列式写入和验证连接；跨盘样本全列一致，完整回归通过。 |
| 2026-11-05 18:46 | [SQLite 到 Parquet 迁移性能优化](PLAN-20261105-005-model-unknown-sqlite-parquet-migration-performance.md) / [结果](PLAN-20261105-005-model-unknown-sqlite-parquet-migration-performance-OUTCOME.md) | 已完成（WORKTREE） | 补充验证 DuckDB 1.1.0，62 项相关测试通过。 |
| 2026-11-05 20:20 | [Web 界面审查与优化](PLAN-20261105-006-model-unknown-web-ui-review.md) | 实施中 | 沿用既有结构，修复浏览、诊断和状态表达问题，优化可访问性与窄屏布局。 |
| 2026-11-05 20:55 | [Web 界面审查与优化](PLAN-20261105-006-model-unknown-web-ui-review.md) / [结果](PLAN-20261105-006-model-unknown-web-ui-review-OUTCOME.md) | 已完成（WORKTREE） | 修复浏览、分区和时间输入问题，改善状态、键盘与窄屏体验；相关回归通过，独立 PostgreSQL 测试跳过。 |
| 2026-11-05 21:30 | [Web 大表按需加载](PLAN-20261105-007-model-unknown-web-lazy-loading.md) | 实施中 | 将总览、分区明细和筛选计数改为按需加载，保留排序和分页语义。 |
| 2026-11-05 21:42 | [Web 大表按需加载](PLAN-20261105-007-model-unknown-web-lazy-loading.md) / [结果](PLAN-20261105-007-model-unknown-web-lazy-loading-OUTCOME.md) | 已完成（WORKTREE） | 首屏仅请求数据页，统计按需执行；实际库副本和相关回归通过，记录有序首页剩余成本。 |
| 2026-11-05 21:47 | [Web 大表按需加载](PLAN-20261105-007-model-unknown-web-lazy-loading.md) | 补充验证中 | 按用户要求手动测试原库，修复计数成功状态多余的 `null` 文本。 |
| 2026-11-05 22:07 | [Web 大表按需加载](PLAN-20261105-007-model-unknown-web-lazy-loading.md) / [结果](PLAN-20261105-007-model-unknown-web-lazy-loading-OUTCOME.md) | 已完成（WORKTREE） | 指定原库手动测试通过，修复计数显示、响应式状态栏、筛选链接与 6 小时分区跳转。 |
| 2026-11-05 22:13 | [Web 大表按需加载](PLAN-20261105-007-model-unknown-web-lazy-loading.md) / [结果](PLAN-20261105-007-model-unknown-web-lazy-loading-OUTCOME.md) | 已完成（WORKTREE） | 修复筛选防抖期间快速切换导致旧页复用；原库再次手测和 133 项回归通过。 |
