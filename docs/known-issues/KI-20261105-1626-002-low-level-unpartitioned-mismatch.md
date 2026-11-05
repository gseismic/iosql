# KI-20261105-1626-002：低层写入可向无分区旧表添加分区并造成契约不一致

- 创建时间：2026-11-05 16:26（Asia/Shanghai）
- 模型：Codex，环境说明其基于 GPT-6；未提供可核实的实际模型 ID。
- 初始状态：未解决。
- 来源：用户询问已有表继续写入时的分区行为，本次任务复核分区优化项。
- 确认版本：`a803d0ea4f6e476f7a4b11842996ed025259992a`；引入提交未知。

## 2026-11-05 16:26 确认

SQLite 和 Parquet 的已有无分区表写入过数据后，通过低层 `insert_all(..., partition=Partition.duration(...))` 指定分区，现有检查只在旧分区元数据非空时拒绝变化（`iosql/engines/sqlite/table.py:5014`、`iosql/engines/parquet/table.py:380`），因而可能接受新分区。新文件创建时更新物理分区元数据，而表契约仍为无分区（SQLite 路径见 `iosql/engines/sqlite/table.py:5061`）。

隔离临时库复现：先用无分区表写入 `ts=0`，再用低层 `insert_all` 按小时写入 `ts=7200000`；写入成功，`spec().partition` 仍为 `None`，底层分区元数据已是小时分区。此后普通 `write()` 抛 `TableSpecConflict`，重开后契约与物理元数据仍不一致。SQLite 和 Parquet、分别有无 row segment 的四种组合均复现。

影响：表布局和契约分裂，后续正常写入受阻。临时绕过：对已有表使用 `write()` 沿用契约；需要调整分区时迁移到新目标，而不是在低层写入中切换分区。

## 2026-11-05 16:57 开始处理

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“未解决”变为“处理中”：用户要求修复全部五项分区问题。计划为 `docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`，设计为 `docs/design/partition-integrity-20261105-fixes.md`；分析基线为 `WORKTREE`，基准提交 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

## 2026-11-05 18:20 已解决

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“处理中”变为“已解决”。修复版本为 `WORKTREE`，当前基准提交 `35275e989349ce55a65268f0da6d5fbb5c3300b9`；本任务开始时的基准为 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

低层写入在表锁内同时核对持久分区和声明契约，包括原表无分区的情形；不一致时拒绝写入，避免修改元数据或落入新物理文件。

验证：隔离 PostgreSQL 18 全量套件 1341 passed、19 skipped；另单独验证 PostgreSQL 向 SQLite/Parquet 迁移日期路由。针对首次写入、重开、冲突无副作用、旧布局、跨年及不同输入形式的回归见 tests/test_partition_integrity.py 和 tests/test_partition_integrity_postgresql.py。

计划：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`；结果：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity-OUTCOME.md`。

## 2026-11-05 18:23 补充验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准仍为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。上述全量测试完成后，其他并行任务修改了 `iosql/engines/parquet/table.py` 并新增 `iosql/engines/parquet/_bulk.py`；当前工作区重新运行分区、用户接口和时序契约测试 183 passed，以及真实 PostgreSQL 分区和安全迁移测试 55 passed。状态不变。

## 2026-11-05 18:25 跨后端验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。并行工作随后调整了 `iosql/migration.py` 并新增 `iosql/_migration_sqlite_parquet.py`；当前工作区上旧日期路由的双向跨后端升级及重开测试 4 passed，迁移与安全迁移测试 60 passed。状态不变。
