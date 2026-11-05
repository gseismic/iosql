# KI-20261105-1626-001：强制修改已有表分区后可能漏查及产生重复主键

- 创建时间：2026-11-05 16:26（Asia/Shanghai）
- 模型：Codex，环境说明其基于 GPT-6；未提供可核实的实际模型 ID。
- 初始状态：未解决。
- 来源：用户询问已有表继续写入时的分区行为，本次任务复核分区优化项。
- 确认版本：`a803d0ea4f6e476f7a4b11842996ed025259992a`；引入提交未知。

## 2026-11-05 16:26 确认

在已有数据的 SQLite 或 Parquet 表上，通过 `db.table(..., force_partition=True)` 声明不同分区时，`iosql/database.py:403` 跳过分区兼容性检查，`iosql/database.py:465` 写回新分区契约，但旧记录不会按新契约搬迁。查询按当前分区计算候选文件（`iosql/engines/sqlite/table.py:4060`），而单个物理文件内的主键约束不能覆盖旧、新分区。

隔离临时库复现：原表按天分区，写入 `ts=7200000`，强制改为按小时后，不带过滤条件的查询仍能读取旧行，`ts == 7200000` 的精确查询返回空；再对相同 `ts` 以 `mode='upsert'` 写入，全表出现两行同主键。SQLite 和 Parquet 均复现。

影响：历史记录可能被分区裁剪漏掉；更新可能在不同物理文件产生重复主键，且重开库后仍存在。临时绕过：已有数据时避免 `force_partition=True`；正常变更使用新目标迁移，并在切换前核对数据。若已使用强制变更，应先按物理布局核对历史数据与主键，再决定恢复方式。

## 2026-11-05 16:57 开始处理

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“未解决”变为“处理中”：用户要求修复全部五项分区问题。计划为 `docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`，设计为 `docs/design/partition-integrity-20261105-fixes.md`；分析基线为 `WORKTREE`，基准提交 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

## 2026-11-05 18:20 已解决

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“处理中”变为“已解决”。修复版本为 `WORKTREE`，当前基准提交 `35275e989349ce55a65268f0da6d5fbb5c3300b9`；本任务开始时的基准为 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

SQLite/Parquet 在共享写锁内核验已有物理布局；已有分区时拒绝原地切换，仅空表允许显式 force。深度校验可报告历史错位行，历史混合布局需全表迁移并处理可能的主键冲突。

验证：隔离 PostgreSQL 18 全量套件 1341 passed、19 skipped；另单独验证 PostgreSQL 向 SQLite/Parquet 迁移日期路由。针对首次写入、重开、冲突无副作用、旧布局、跨年及不同输入形式的回归见 tests/test_partition_integrity.py 和 tests/test_partition_integrity_postgresql.py。

计划：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`；结果：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity-OUTCOME.md`。

## 2026-11-05 18:23 补充验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准仍为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。上述全量测试完成后，其他并行任务修改了 `iosql/engines/parquet/table.py` 并新增 `iosql/engines/parquet/_bulk.py`；当前工作区重新运行分区、用户接口和时序契约测试 183 passed，以及真实 PostgreSQL 分区和安全迁移测试 55 passed。状态不变。

## 2026-11-05 18:25 跨后端验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。并行工作随后调整了 `iosql/migration.py` 并新增 `iosql/_migration_sqlite_parquet.py`；当前工作区上旧日期路由的双向跨后端升级及重开测试 4 passed，迁移与安全迁移测试 60 passed。状态不变。

## 2026-11-05 18:50 修复提交

模型：Codex，基于 GPT-6；实际模型 ID 未提供。已验证的修复版本为提交 `0bf5c9af250cde1a0a281e42eb3a3b78bd4c895e`；提交快照的隔离 PostgreSQL 18 全量测试为 1345 passed、19 skipped。状态保持“已解决”。本条为提交后的追加记录，尚未另行提交。
