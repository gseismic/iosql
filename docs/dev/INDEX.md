# 开发计划索引

既有历史计划保留在本目录，此索引从当前任务开始维护。

| 更新时间（Asia/Shanghai） | 计划 | 状态 | 结果 |
| --- | --- | --- | --- |
| 2026-10-23 | [PLAN-093 全库 Review 修复](PLAN-093-full-review-fixes.md) | 已完成 | [结果](PLAN-093-full-review-fixes-OUTCOME.md)：修复 R01–R17 与 3 项已知问题；完整回归 1111 通过、19 跳过。 |
| 2026-10-18 11:04 | [PLAN-083 统一迁移参数与安全发布](PLAN-083-model-unknown-safe-migration.md) | 实施中 | 设计已补充，开始锁与迁移执行器实现。 |

| 2026-10-18 12:43 | [PLAN-083 统一迁移参数与安全发布](PLAN-083-model-unknown-safe-migration.md) | 已完成（WORKTREE） | [结果](PLAN-083-model-unknown-safe-migration-OUTCOME.md)：统一 tables/specs 与安全发布；完整回归 880 通过，6 个基线失败，15 跳过。 |
| 2026-10-18 15:39 | [PLAN-084 迁移审查问题修复](PLAN-084-model-unknown-migration-review-fixes.md) | 实施中 | 修复退休路径竞态、快照漏表、Windows 锁、空表声明、关闭状态、秒级裁剪及 ui 别名。 |
| 2026-10-18 15:55 | [PLAN-084 迁移审查问题修复](PLAN-084-model-unknown-migration-review-fixes.md) | 已完成（WORKTREE） | [结果](PLAN-084-model-unknown-migration-review-fixes-OUTCOME.md)：新增 47 项及针对性 336 项通过；完整回归 927 通过、6 项原有失败、15 跳过；Windows 未原机验证。 |
| 2026-10-18 | [PLAN-085 Windows 库锁文件重解析点防护](PLAN-085-windows-lock-reparse-points.md) | 已完成（WORKTREE） | [结果](PLAN-085-windows-lock-reparse-points-OUTCOME.md)：锁回归 28 通过、2 跳过；扩展回归 344 通过、16 跳过、2 项既有失败；Windows 未原机验证。 |
| 2026-10-18 | [PLAN-086 Windows 路径大小写与快照重解析点防护](PLAN-086-windows-paths-and-snapshot-junctions.md) | 已完成（WORKTREE） | [结果](PLAN-086-windows-paths-and-snapshot-junctions-OUTCOME.md)：文件路径保留大小写；快照拒绝重解析点；回归 330 通过、6 项 Windows 原机用例跳过。 |
| 2026-10-18 | [PLAN-087 路径安全示例与审查修复提交](PLAN-087-path-safety-example-and-commit.md) | 已完成（随本轮修复提交） | [结果](PLAN-087-path-safety-example-and-commit-OUTCOME.md)：主示例及技能副本运行通过，同步索引，提交 PLAN-084～087 相关代码与文档。 |
| 2026-10-18 | [PLAN-088 README 与 iosql skill 同步](PLAN-088-readme-skill-sync.md) | 已完成 | [结果](PLAN-088-readme-skill-sync-OUTCOME.md)：补齐三后端能力、01～17 示例、元数据与迁移结果/异常说明；链接、示例副本、代码块语法和 skill 格式检查通过。 |
| 2026-10-18 | [PLAN-089 保留源库的 save_as 接口](PLAN-089-save-as.md) | 已完成（随本次提交） | [结果](PLAN-089-save-as-OUTCOME.md)：新增库/表另存入口与查询计时示例，同步 README 和 skill；新增 28 项通过，扩展 419 通过、3 项基线失败、2 跳过。 |
| 2026-10-21 17:44 | [PLAN-090 SQLite 写入性能研究](PLAN-090-model-unknown-sqlite-write-research.md) | 研究中（WORKTREE） | 已确认固定结构批量写入中的重复类型扫描热点，继续验证批次、segment 与冲突策略。 |
| 2026-10-21 18:10 | [PLAN-090 SQLite 写入性能研究](PLAN-090-model-unknown-sqlite-write-research.md) | 已完成研究（WORKTREE） | [结果](PLAN-090-model-unknown-sqlite-write-research-OUTCOME.md)：真实 CSV 导入首要耗时是写入前的 legacy 全列统计；当前源码隔离实验整体约 5.5 倍、90 轮通用对照完成。引擎与业务代码未改，两个性能问题已记录待修复。 |
| 2026-10-21 18:27 | [PLAN-091 普通查询取消 legacy 统计预扫描](PLAN-091-model-unknown-query-without-legacy-stat-scans.md) | 实施中（WORKTREE） | 按用户确认的时间分区语义修复查询寻址，保留条件过滤、诊断统计及 row segment 的已有裁剪。 |
| 2026-10-21 19:30 | [PLAN-091 普通查询取消 legacy 统计预扫描](PLAN-091-model-unknown-query-without-legacy-stat-scans.md) | 已完成（WORKTREE） | [结果](PLAN-091-model-unknown-query-without-legacy-stat-scans-OUTCOME.md)：相关回归 271 项通过；真实 CSV 副本导入 0.7624 → 0.1031 秒，保留全主键检查。6 项既有失败经 HEAD 复现，另记录连接池偶发异常。 |
| 2026-10-21 23:31 | [PLAN-091 普通查询取消 legacy 统计预扫描](PLAN-091-model-unknown-query-without-legacy-stat-scans.md) | 已提交（479f8e8） | [结果](PLAN-091-model-unknown-query-without-legacy-stat-scans-OUTCOME.md)：按用户授权提交修复及 PLAN-090 研究记录，补记实际修复提交编号；未推送，测试产物保留本地。 |
