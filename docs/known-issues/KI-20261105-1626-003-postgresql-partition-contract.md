# KI-20261105-1626-003：PostgreSQL 可绑定不同分区粒度并产生混合布局

- 创建时间：2026-11-05 16:26（Asia/Shanghai）
- 模型：Codex，环境说明其基于 GPT-6；未提供可核实的实际模型 ID。
- 初始状态：未解决。
- 来源：用户询问已有表继续写入时的分区行为，本次任务复核分区优化项。
- 确认版本：`a803d0ea4f6e476f7a4b11842996ed025259992a`；引入提交未知。

## 2026-11-05 16:26 确认

普通 PostgreSQL 表的契约存于当前实例；`iosql/engines/postgresql/table.py:531` 重新绑定时检查物理表结构，`iosql/engines/postgresql/table.py:483` 检查分区键和 RANGE 形式，但没有比较原有分区粒度。重开后调用方也须重新提供同一份契约，不能自动从普通表恢复此前的 iosql 契约。

隔离 PostgreSQL 18 临时实例复现：对已写入数据的日分区表，使用相同主键和分区列改为小时契约，未传 `force_partition` 也能绑定成功。向旧日分区覆盖的时段写入时报重叠分区 `BadArgument`；向不重叠的未来时段写入成功，形成日、小时混合布局。重新打开实例并绑定小时契约仍可成功。

影响：分区声明与既有布局可能不一致，写入行为取决于目标时间范围。临时绕过：保留并重新绑定原契约，不在已有表上重新声明不同粒度；确需调整时迁移到新目标。

## 2026-11-05 16:57 开始处理

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“未解决”变为“处理中”：用户要求修复全部五项分区问题。计划为 `docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`，设计为 `docs/design/partition-integrity-20261105-fixes.md`；分析基线为 `WORKTREE`，基准提交 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

## 2026-11-05 18:20 已解决

模型：Codex，基于 GPT-6；实际模型 ID 未提供。状态由“处理中”变为“已解决”。修复版本为 `WORKTREE`，当前基准提交 `35275e989349ce55a65268f0da6d5fbb5c3300b9`；本任务开始时的基准为 `a803d0ea4f6e476f7a4b11842996ed025259992a`。

绑定和写入时校验父表分区摘要、历史控制契约和全部原生子分区边界；事务级 advisory 锁及 PostgreSQL 父表锁防止并发布局变更。旧表首次固定摘要需要表所有者权限。写入开销的后续评估见 TD-20261105-1817-001。

验证：隔离 PostgreSQL 18 全量套件 1341 passed、19 skipped；另单独验证 PostgreSQL 向 SQLite/Parquet 迁移日期路由。针对首次写入、重开、冲突无副作用、旧布局、跨年及不同输入形式的回归见 tests/test_partition_integrity.py 和 tests/test_partition_integrity_postgresql.py。

计划：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity.md`；结果：`docs/plans/PLAN-20261105-002-model-unknown-partition-integrity-OUTCOME.md`。

## 2026-11-05 18:23 补充验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准仍为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。上述全量测试完成后，其他并行任务修改了 `iosql/engines/parquet/table.py` 并新增 `iosql/engines/parquet/_bulk.py`；当前工作区重新运行分区、用户接口和时序契约测试 183 passed，以及真实 PostgreSQL 分区和安全迁移测试 55 passed。状态不变。

## 2026-11-05 18:25 跨后端验证

模型：Codex，基于 GPT-6；实际模型 ID 未提供。`WORKTREE` 基准为 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。并行工作随后调整了 `iosql/migration.py` 并新增 `iosql/_migration_sqlite_parquet.py`；当前工作区上旧日期路由的双向跨后端升级及重开测试 4 passed，迁移与安全迁移测试 60 passed。状态不变。
