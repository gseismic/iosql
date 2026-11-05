# 显式分区参数警告：实施结果

- 完成时间：2026-11-05 16:34（Asia/Shanghai）
- 实际执行模型：Codex，环境说明其基于 GPT-6；未提供可核实的实际模型 ID。
- 实施范围：`WORKTREE`，基准提交 `a803d0ea4f6e476f7a4b11842996ed025259992a`；相关未提交文件包括 `iosql/database.py`、`tests/test_user_api_unify.py`、`README.md`、`skills/iosql-usage/` 下的使用说明、`docs/KNOWN-ISSUES.md`、五个新增问题条目及本计划、结果和索引。以上内容尚未固定为提交。
- 对应计划：`docs/plans/PLAN-20261105-001-model-unknown-explicit-partition-warning.md`。

## 完成情况

- `Database.timeseries()` 显式同时收到非 `None` 的 `partition` 和 `partition_every` 时，在表绑定前发出 `UserWarning`，指出后者的值被忽略、`partition` 决定布局以及消除歧义的方法。即便显式传入的间隔与分区等价，也会提示参数未参与选择。
- 只传 `partition`、只传 `partition_every`、显式传入 `partition=None` 或都不传时，不产生该警告；旧分区选择与公开签名默认值保持不变。将 `UserWarning` 升级为异常时，相关表尚未绑定。
- 新包装保留 `inspect.signature()`；新警告和三类原有 `timeseries()` 废弃参数警告均指向调用方。
- README 和仓库内 iosql 使用参考说明新警告，并对已复现的分区风险补充临时用法。

## 验证结果

- SQLite、Parquet、时序、分区及 mysqlite 相关回归：`366 passed, 68 warnings in 76.92s`；warning 为既有弃用提示和环境中的 `fork()` 提示。
- 调整原有废弃警告的定位后，重跑用户接口与 SQLite 时序测试：`94 passed, 35 warnings in 3.23s`。
- 最终收紧警告文案后，重跑用户接口测试：`44 passed in 1.49s`。
- PostgreSQL 18 隔离临时实例：确认新 `UserWarning`、实际小时分区及写入后查询均正常；临时实例已关闭并清理。
- `git diff --check` 通过；README、计划索引和问题索引等相关本地链接检查通过。

## 计划偏差和遗留问题

自查发现新增包装会影响原有废弃警告的定位，已调整 `stacklevel` 并补充调用方定位测试。复核时发现 calendar 字符串与对象的时序契约不一致，以及 PostgreSQL 多日 calendar 跨年边界重叠；按更新后的计划补充使用文档及已知问题记录。

本次未修改其他分区算法或迁移行为。确认后仍需跟踪的既有问题：

- `KI-20261105-1626-001`：强制切换已有表分区可能漏查和产生重复主键。
- `KI-20261105-1626-002`：低层写入可能造成无分区旧表的契约和物理布局分裂。
- `KI-20261105-1626-003`：PostgreSQL 可能接受不同粒度并形成混合布局或写入失败。
- `KI-20261105-1626-004`：PostgreSQL 多日 calendar 分区的跨年边界可能重叠。
- `KI-20261105-1628-001`：`timeseries()` 的 calendar 字符串可能导致时间契约错误和日期查询漏查。

详情与临时绕过方式见 `docs/KNOWN-ISSUES.md`。本次没有提交或推送。
