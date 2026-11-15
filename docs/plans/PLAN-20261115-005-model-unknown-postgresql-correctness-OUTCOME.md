# PostgreSQL 逻辑正确性循环审查与修复结果

创建时间：2026-11-15 16:49（Asia/Shanghai）\
执行模型：Codex（环境说明基于 GPT-6，未提供可核实的实际模型 ID）\
分析及修复版本：WORKTREE；基准提交 `620ee5e040159cb458c2fb60068e6352ee07b355`。实现、测试、README、仓库 skill 说明与本轮计划/结果等文件未提交，不能将基准 HEAD 视为修复后的固定版本。

相关未提交 Python 文件：`iosql/engines/postgresql/table.py`、`iosql/engines/postgresql/query.py`、`iosql/core/query/timeseries.py`、`tests/engines/postgresql/test_correctness.py`、`tests/test_plan062_pg.py`、`tests/test_partition_integrity_postgresql.py`、`tests/test_migration.py`、`tests/test_plan093_resources.py`。另涉及 `README.md`、`skills/iosql-usage/references/api-reference.md`、`skills/iosql-usage/references/design-contract.md`、本轮计划/结果与计划索引、TODOS 索引和本轮 TODO，以及关联 KNOWN-ISSUES 详情的追加事件。

计划：[PLAN-20261115-005-model-unknown-postgresql-correctness.md](PLAN-20261115-005-model-unknown-postgresql-correctness.md)。

## 完成情况

已修复前轮五项逻辑问题，并在后续复审中处理同范围的事务清理、输入覆盖顺序、实际写入计数、特殊标识符和超长名称串表问题。公开 Table/Query 方法签名与基准版本一致，新增的是内部辅助方法。

| 事项 | 最终行为与代码依据 |
| --- | --- |
| 事务中断线后误用自动提交 | 重连发现未提交事务丢失时清理状态和租约，当前操作抛 UnsafeOperation；调用方明确重新开始事务。迁移专用连接禁止自行重连。`iosql/engines/postgresql/table.py:182` |
| SQL、提交或回滚失败清理不完整 | 提交失败进入统一清理，rollback/close 的清理错误不覆盖原错误；普通写事务和自有流式读取均保留断连错误。提交回复丢失的持久结果需查询确认，不自动重放。`iosql/engines/postgresql/table.py:283`、`iosql/engines/postgresql/table.py:302` |
| delete 未提交待处理事务 | delete/delete_all 统一经过事务入口；成功提交待处理写入，失败回滚整笔事务；迁移发布器仍保留提交所有权。`iosql/engines/postgresql/table.py:2083`、`iosql/engines/postgresql/table.py:2116` |
| 重复主键依赖 SQL 分页 | 在 SQL 类型归一后按每个键的最后输入行取值，跨页依次覆盖；完整行写入按批次列并集缺列补 NULL。WriteResult 使用数据库 rowcount，避免去重或 DO NOTHING 后高估写入数。`iosql/engines/postgresql/table.py:991` |
| 字段级 upsert 覆盖顺序 | 按连续的相同字段集合批量执行，不再按 Python 原始主键全局重排；SQL 等价键的字段覆盖保持输入顺序，缺字段保留，显式 None 清空。`iosql/engines/postgresql/table.py:2044` |
| 文本时间起点精度丢失 | 下界向上、上界向下对齐统一文本时间格式的精度；覆盖秒、分钟、小数秒、compact、跨日及时区，谓词仍直接作用于列并保留分区裁剪。原生日期及其他引擎保留既有规则。`iosql/engines/postgresql/query.py:68`、`iosql/core/query/timeseries.py:125` |
| 布局锁不能排斥 ATTACH | 父表 SHARE UPDATE EXCLUSIVE 锁覆盖布局校验到写入，阻止并发 ATTACH/DETACH；普通 SELECT 可继续。缺少必要锁权限时明确拒绝。`iosql/engines/postgresql/table.py:859` |
| 特殊和超长标识符 | 有参数与无参数的 SQL 分别处理百分号转义；表、列和重命名入口拒绝超过 63 个 UTF-8 字节的名称，防止不同逻辑表被截断到同一物理表。`iosql/engines/postgresql/table.py:88`、`iosql/engines/postgresql/query.py:33` |

主要新增真实数据库反例为 `tests/engines/postgresql/test_correctness.py`；轻量错误路径补充在 `tests/test_plan062_pg.py`。已有权限回归和迁移 SQL 测试桩同步更新，后者补齐驱动的 rowcount 属性。

## 验证结果

最终验证环境为 macOS、Python 3.11.15、PostgreSQL 14.19（Homebrew）、psycopg2 2.9.10、Arrow 1.3.0、DuckDB 1.5.4、pytest 8.4.2、FastAPI 0.121.0、Typer 0.12.5、Click 8.1.8。全量验证使用 HEAD 归档叠加最终工作区改动的独立副本，并在本次临时实例内使用专用数据库，显式设置 POSTGRES_DSN 和 IOSQL_EXAMPLE_TEST_POSTGRES_DSN。

| 检查 | 实际结果 |
| --- | --- |
| 修改实现前的首轮真实 PG 反例 | 21 failed、4 passed，证明原五项及相邻失败路径；不是通过结果 |
| 最终工作区 PostgreSQL/分区/安全迁移/迁移 SQL 专项 | 235 passed，0 skipped |
| 中间 SQLite/partition/mysqlite 必要回归 | 326 passed、1 skipped |
| 确定性读取超时与资源释放反例 | SQLite、Parquet 两个参数化用例通过 |
| 最终完整回归 `python -m pytest -q tests --disable-warnings --tb=short -rs -p no:cacheprovider` | **1599 passed、99 skipped，0 failed、0 errors**；XML 共 1698 个用例，约 496 秒 |
| 修改文件 ruff、git diff --check | 通过 |
| Table/Query 公开方法 AST 签名与 HEAD 对比 | 无签名变化、无新增公开入口 |
| 验证副本和最终工作区核对 | 8 个实现/测试 Python 文件及 README、两份 skill 说明共 11 个文件 SHA-256 一致 |

99 个跳过项包括已废弃测试、未配置独立密码认证环境、缺少 Playwright Chromium、Windows 专属行为、文件系统及 `/proc` 条件；不把这些场景报告为已验证。实际 PostgreSQL 集成验证使用 14.19，其他主版本本轮未实机执行。

此前中止或失败的回归未被计为通过：旧 FakeCursor 缺少 rowcount、CLI 依赖兼容性、以及共享读取测试假定首批一定能在 20ms 内完成的问题均已分别定位。最后一项只调整测试的时间驱动，使用首批后的受控 monotonic 推进验证截止及资源释放；生产 SQLite/Parquet 代码未因此改变。最后一轮完整回归在这些处理以及流式断连、标识符长度修复全部完成后重新执行，验证的是最终代码。

## 计划偏差与兼容性

- 复审比原五项扩展到同范围内的提交失败、读取流断连、SQL 等价键、rowcount、百分号及 63 字节名称边界；均有代码和反例依据，未修改公开方法签名。
- 分区写入角色需要表级 UPDATE、DELETE 或 TRUNCATE 权限之一，才能取得新的布局锁；仅 INSERT 不够。库不自动授权，首次固定分区摘要仍需所有者身份。依据为 [PostgreSQL 14 LOCK 权限规则](https://www.postgresql.org/docs/14/sql-lock.html)。已有超长别名访问应改用实际物理表名，库不再接受会被静默截断的名称。
- 同字段形状仍批量执行；字段形状交替的 upsert_all 可能产生更多 SQL。本轮以正确性为主，未运行百万行吞吐基准。
- 在未修改 HEAD 上，Typer 0.19.2/Click 8.3.0 也会导致三项旧 CLI 测试失败；保持核心依赖版本后使用上述较早组合，对应 HEAD 用例及最终全量均通过。该范围外兼容性改进记录为待评估 [TD-20261115-1621-001](../todos/TD-20261115-1621-001-cli-test-dependency-compatibility.md)，未将其描述为 PostgreSQL 修复引入的问题。
- 已在 [KI-20261105-1626-003](../known-issues/KI-20261105-1626-003-postgresql-partition-contract.md) 追加布局锁补充复核结果，保留原条目与原状态。

## 最后一轮复审与工作区

最后一轮重新核对事务生命周期、嵌套事务、提交不确定性、删除、输入覆盖、类型与分页、时区和精度、分区布局、迁移提交所有权、标识符及资源释放，并核对最终测试证据和源码一致性。该轮未再修改实现或测试，本次 PostgreSQL 审查范围内未发现待修复的逻辑问题；这不等于证明绝对无错误。

临时 PostgreSQL 实例已停止。工作区为 WORKTREE，未执行 commit 或 push；原有 `.playwright-cli/` 未改动，GOAL.md 未创建或修改。

## 2026-11-15 17:11 修复提交与示例交接

模型：Codex（基于 GPT-6，实际模型 ID 未提供）。上述已验证的实现与测试已提交为 `3cb4c8baf226bb53715f2cf8f4bc22aed77f00ad`；该提交对应本结果先前记录的 WORKTREE 内容，不把创建时基准误认为修复版本。用户已明确要求先提交并写交接，未要求推送，当前未推送。

用户新增的示例规范检查尚未开始，接续范围和权限问题解释记录于 `docs/HANDOFF.md`。本次仅追加提交依据与交接记录，不改变已验证实现或测试。
