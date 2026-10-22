# KI-20261021-1930-001：组合回归中连接池预算遍历偶发异常

- 创建：2026-10-21 19:30（Asia/Shanghai）。模型：Codex，精确模型 ID 未提供，记为 `model-unknown`。
- 初始状态：未解决。
- 当前状态：已修复（PLAN-093，2026-10-23）。
- 来源：PLAN-091 相关回归，不属于本轮性能修复范围；保留观察记录以供后续定位。
- 确认版本：WORKTREE，基准 `5f6ff5b17ae9e23948d3f3697bf411c1b555c999`；`iosql/utils/mysqlite/connection_pool.py` 与该提交完全一致。没有定位引入提交，也没有证明性能修复引入此异常。

## 观察到的行为

在 SQLite/Parquet/partition/mysqlite 的 362 项组合回归中，`tests/test_plan062_sqlite.py::test_missing_segment_file_raises_and_is_not_recreated` 初始化写入时，经连接池 `_create_connection()` → `_ProcessConnectionBudget.acquire()` → `_effective_limit_locked()` 抛出 `RuntimeError: dictionary changed size during iteration`。

栈落在 `iosql/utils/mysqlite/connection_pool.py:73` 对 `self._pools.items()` 的迭代，底层是 `WeakKeyDictionary`。同文件 `:275` 的析构会调用强制关闭，`:584` 显式 unregister 会修改字典；是否与当时 GC/析构时机有关尚未验证，不能据此认定根因。

影响是连接创建失败，进而让正常表写入操作报错。当前只有一次该异常的日志，没有稳定复现步骤。

## 验证与后续边界

- 原始记录：`docs/tests/20261021-18-WORKTREE/pytest-targeted-final.log`，命令范围和环境见同目录 README。
- 缺文件用例与进程预算回收用例单独复跑 2 项通过；最终 SQLite/Parquet/partition 271 项组合全部通过。此前扩展回归的 944 个通过用例也未出现该异常，不能把未复现当作已经解决。
- 性能修复未修改连接池。后续应针对池的 GC、析构和显式注销与预算迭代的交互建立稳定复现，再决定修复方式；当前没有经过验证的业务绕过方案。
- 本轮未修复。关联结果：`docs/dev/PLAN-091-model-unknown-query-without-legacy-stat-scans-OUTCOME.md`。

## 2026-10-23：PLAN-093 修复

预算计算先复制弱引用注册表的底层映射，再解引用并建立强引用快照，避免 GC/析构显式注销修改正在遍历的字典。回收候选池也使用同一快照原语。

`test_budget_snapshot_survives_reentrant_unregistration` 在读取池状态时注销另一个池，稳定覆盖原迭代失效条件；`test_budget_pool_lifecycle_is_safe_under_gc_and_threads` 让四个线程反复创建、查询、关闭并触发 GC，验证并发生命周期。两者均通过，现有 fork、预算回收与完整回归也通过；不再以“单次未复现”作为关闭问题的依据。
