# KI-20261105-1628-001：timeseries 的 calendar 字符串导致时间契约错误

- 创建时间：2026-11-05 16:28（Asia/Shanghai）
- 模型：Codex，环境说明其基于 GPT-6；未提供可核实的实际模型 ID。
- 初始状态：未解决。
- 来源：本次任务检查分区接口，用户要求了解可优化项。
- 确认版本：`WORKTREE`，基准提交 `a803d0ea4f6e476f7a4b11842996ed025259992a`；本次 `iosql/database.py` 含警告包装改动，原有分区判断和时间列构造逻辑未修改。引入提交未知。

## 2026-11-05 16:28 确认

`Database.timeseries()` 通过 `iosql/database.py` 中 `_is_calendar_partition()` 检查对象的 `PARTITION_TYPE`，对 `calendar(...)` 字符串不先解析，就按 duration 场景构造毫秒 `TimeColumn`；等价的 `Partition.calendar(...)` 对象则构造日期单位并采用分区时区。代码依据：`iosql/database.py:26`、`iosql/database.py:592`（本次工作区行号）。

隔离临时库复现：对日期文本列 `d`，以字符串 `calendar(d,every=1day,timezone=Asia/Shanghai)` 创建时序表并写入 `2025-01-01`。`spec().time` 被记为 `unit='ms', timezone='UTC'`；全表读取能看到记录，但 SQLite 的 `query(date='2025-01-01')` 返回空，Parquet 的同一查询因字符串与整数比较抛 `BadArgument`。用等价 `Partition.calendar(...)` 对象创建时，时间单位为 `date`，时区为 `Asia/Shanghai`，两个后端均能按日期查到记录。

影响：合法的日期查询可能静默漏查或失败，时区契约也可能错误并随表持久化。临时绕过：创建日期时序表时使用 `Partition.calendar(...)` 对象；对已使用字符串创建的表，先检查 `table.spec().time` 并核实数据，不能仅重新打开就假定契约已修正。

## 2026-11-05 16:57 开始处理

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“未解决”变为“处理中”：用户要求修复全部五项分区问题。计划为 `docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`，设计为 `docs/design/partition-integrity-20261105-fixes.md`；分析基线为 `WORKTREE`，基准提交 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

## 2026-11-05 18:20 已解决

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“处理中”变为“已解决”。修复版本为 `WORKTREE`，当前基准提交 `35275e989349ce55a65268f0da6d5fbb5c3300b9`；本任务开始时的基准为 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

timeseries 先统一解析分区字符串、对象、字典和 PartitionSpec，再根据日历类型推导日期单位及业务时区；保留显式 TimeColumn。旧版错误时间契约不盲改，提供全表复制核验的恢复说明。

验证：隔离 PostgreSQL 18 全量套件 1341 passed、19 skipped；另单独验证 PostgreSQL 向 SQLite/Parquet 迁移日期路由。针对首次写入、重开、冲突无副作用、旧布局、跨年及不同输入形式的回归见 tests/test_partition_integrity.py 和 tests/test_partition_integrity_postgresql.py。

计划：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`；结果：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity-OUTCOME.md`。

## 2026-11-05 18:23 补充验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准仍为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。上述全量测试完成后，其他并行任务修改了 `iosql/engines/parquet/table.py` 并新增 `iosql/engines/parquet/_bulk.py`；当前工作区重新运行分区、用户接口和时序契约测试 183 passed，以及真实 PostgreSQL 分区和安全迁移测试 55 passed。状态不变。

## 2026-11-05 18:25 跨后端验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。并行工作随后调整了 `iosql/migration.py` 并新增 `iosql/_migration_sqlite_parquet.py`；当前工作区上旧日期路由的双向跨后端升级及重开测试 4 passed，迁移与安全迁移测试 60 passed。状态不变。

## 2026-11-05 18:50 修复提交

模型：Codex，基于 GPT-6；实际模型 ID 未提供。已验证的修复版本为提交 `0bf5c9af250cde1a0a281e42eb3a3b78bd4c895e`；提交快照的隔离 PostgreSQL 18 全量测试为 1345 passed、19 skipped。状态保持“已解决”。本条为提交后的追加记录，尚未另行提交。
