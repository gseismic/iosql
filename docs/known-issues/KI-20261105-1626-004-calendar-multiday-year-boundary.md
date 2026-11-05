# KI-20261105-1626-004：多日 calendar 分区跨年时可能产生重叠边界

- 创建时间：2026-11-05 16:26（Asia/Shanghai）
- 模型：Codex，环境说明其基于 GPT-6；未提供可核实的实际模型 ID。
- 初始状态：未解决。
- 来源：本次任务检查分区正确性，用户要求了解可优化项。
- 确认版本：`WORKTREE`，基准提交 `a803d0ea4f6e476f7a4b11842996ed025259992a`；本次 `iosql/database.py` 含警告改动，所检查的 calendar 分桶和 PostgreSQL 边界代码未修改。引入提交未知。

## 2026-11-05 16:26 确认

`iosql/partition/calendar.py:159` 对不超过 366 天的日历多日间隔按每年 1 月 1 日重新锚定，但 `get_range()` 和 PostgreSQL 边界计算分别在 `iosql/partition/calendar.py:222`、`iosql/engines/postgresql/table.py:1048` 直接按完整间隔向后平移，未截断到下一年度锚点。

隔离复现：UTC 的 `calendar(d,every=4day)` 将 `2024-12-31` 映射到 `[2024-12-30, 2025-01-03)`，将 `2025-01-01` 映射到 `[2025-01-01, 2025-01-05)`，两者重叠。在 PostgreSQL 18 临时实例中，先写前者后写后者，第二次写入因原生分区范围重叠而抛 `BadArgument`。

影响：满足上述边界条件的多日分区表无法正常写入下一年的相应数据；影响范围取决于周期和年份。临时绕过：使用 `1day` 等已核对边界的布局，或迁移到经过验证的新布局；不要直接修改已有表的分区元数据。

## 2026-11-05 16:57 开始处理

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“未解决”变为“处理中”：用户要求修复全部五项分区问题。计划为 `docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`，设计为 `docs/design/partition-integrity-20261105-fixes.md`；分析基线为 `WORKTREE`，基准提交 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

## 2026-11-05 18:20 已解决

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“处理中”变为“已解决”。修复版本为 `WORKTREE`，当前基准提交 `35275e989349ce55a65268f0da6d5fbb5c3300b9`；本任务开始时的基准为 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

按年锚定的多日尾桶在次年起点截断，运行时和 PostgreSQL 共用边界。旧跨年子分区仅在越界区域无数据时事务性收窄，否则拒绝并提示迁移。

验证：隔离 PostgreSQL 18 全量套件 1341 passed、19 skipped；另单独验证 PostgreSQL 向 SQLite/Parquet 迁移日期路由。针对首次写入、重开、冲突无副作用、旧布局、跨年及不同输入形式的回归见 tests/test_partition_integrity.py 和 tests/test_partition_integrity_postgresql.py。

计划：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`；结果：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity-OUTCOME.md`。

## 2026-11-05 18:23 补充验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准仍为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。上述全量测试完成后，其他并行任务修改了 `iosql/engines/parquet/table.py` 并新增 `iosql/engines/parquet/_bulk.py`；当前工作区重新运行分区、用户接口和时序契约测试 183 passed，以及真实 PostgreSQL 分区和安全迁移测试 55 passed。状态不变。

## 2026-11-05 18:25 跨后端验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。并行工作随后调整了 `iosql/migration.py` 并新增 `iosql/_migration_sqlite_parquet.py`；当前工作区上旧日期路由的双向跨后端升级及重开测试 4 passed，迁移与安全迁移测试 60 passed。状态不变。
