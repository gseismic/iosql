# 开发计划索引

既有历史计划保留在本目录，此索引从当前任务开始维护。

| 更新时间（Asia/Shanghai） | 计划 | 状态 | 结果 |
| --- | --- | --- | --- |
| 2026-10-18 11:04 | [PLAN-083 统一迁移参数与安全发布](PLAN-083-model-unknown-safe-migration.md) | 实施中 | 设计已补充，开始锁与迁移执行器实现。 |

| 2026-10-18 12:43 | [PLAN-083 统一迁移参数与安全发布](PLAN-083-model-unknown-safe-migration.md) | 已完成（WORKTREE） | [结果](PLAN-083-model-unknown-safe-migration-OUTCOME.md)：统一 tables/specs 与安全发布；完整回归 880 通过，6 个基线失败，15 跳过。 |
| 2026-10-18 15:39 | [PLAN-084 迁移审查问题修复](PLAN-084-model-unknown-migration-review-fixes.md) | 实施中 | 修复退休路径竞态、快照漏表、Windows 锁、空表声明、关闭状态、秒级裁剪及 ui 别名。 |
| 2026-10-18 15:55 | [PLAN-084 迁移审查问题修复](PLAN-084-model-unknown-migration-review-fixes.md) | 已完成（WORKTREE） | [结果](PLAN-084-model-unknown-migration-review-fixes-OUTCOME.md)：新增 47 项及针对性 336 项通过；完整回归 927 通过、6 项原有失败、15 跳过；Windows 未原机验证。 |
| 2026-10-18 | [PLAN-085 Windows 库锁文件重解析点防护](PLAN-085-windows-lock-reparse-points.md) | 已完成（WORKTREE） | [结果](PLAN-085-windows-lock-reparse-points-OUTCOME.md)：锁回归 28 通过、2 跳过；扩展回归 344 通过、16 跳过、2 项既有失败；Windows 未原机验证。 |
| 2026-10-18 | [PLAN-086 Windows 路径大小写与快照重解析点防护](PLAN-086-windows-paths-and-snapshot-junctions.md) | 已完成（WORKTREE） | [结果](PLAN-086-windows-paths-and-snapshot-junctions-OUTCOME.md)：文件路径保留大小写；快照拒绝重解析点；回归 330 通过、6 项 Windows 原机用例跳过。 |
| 2026-10-18 | [PLAN-087 路径安全示例与审查修复提交](PLAN-087-path-safety-example-and-commit.md) | 已完成（随本轮修复提交） | [结果](PLAN-087-path-safety-example-and-commit-OUTCOME.md)：主示例及技能副本运行通过，同步索引，提交 PLAN-084～087 相关代码与文档。 |
| 2026-10-18 | [PLAN-088 README 与 iosql skill 同步](PLAN-088-readme-skill-sync.md) | 已完成 | [结果](PLAN-088-readme-skill-sync-OUTCOME.md)：补齐三后端能力、01～17 示例、元数据与迁移结果/异常说明；链接、示例副本、代码块语法和 skill 格式检查通过。 |
