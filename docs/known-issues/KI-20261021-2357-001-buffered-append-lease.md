# KI-20261021-2357-001：逐行缓冲调用重复解析协作资源路径

- 创建：2026-10-21 23:57（Asia/Shanghai）。模型：Codex，精确模型 ID 未提供，记为 `model-unknown`。
- 初始状态：未解决。
- 当前状态：已修复（PLAN-093，2026-10-23）。
- 来源：用户要求 profile 仍然较慢的 CSV 恢复导入，PLAN-092 的整月隔离回放。
- 确认版本：`c329cd07958f890b483b78e8603880f86d62aab6`；引入提交未追溯。本轮没有修改库或业务脚本。

## 行为、影响和证据

业务 `export_to_iosql.py:203` 对每行调用 `buf.append(row)`。`iosql/engines/sqlite/table.py:254` 的 `_BufferedWriter` 使用 `guard_query`，其 `append()`（`:268`）即使只将行加入内存缓冲，也经 `iosql/coordination.py:464` 的包装进入 `operation/lease/resource`；`resource()`（`:74`）重复解析文件路径。

43,200 行、120 个已有空物理表、保留完整分区元数据的 NVMe 回放中，cProfile 记录约 43,479 次 `resource()`、43,725 次 `realpath()` 和 131,782 次 `lstat()`。用户侧已经持有 buffered_writer 上下文，逐行调用仍放大了包装和路径解析成本。

未启用 cProfile 的 3 轮中位数：原导入 `1.4557 秒`、CPU `1.2612 秒`；仅在隔离进程的业务模块中改用已有批量入口 `buf.append(rows)`，降到 `0.9229 秒`、CPU `0.7174 秒`。全部主键核验通过，写入仍为 120 次分区 INSERT/commit，没有绕过库的批量 API。

## 绕过与边界

业务可按既有 batch_size 将列表传给 `append()`，减少公开入口调用次数；本次月 CSV 最多 44,640 行，小于业务配置的 50,000。通用实现仍应按批次切片，避免一次追加超大列表放大缓冲。

库内改进须保留关闭检查、迁移代际观察和租约语义，不能直接删除协作包装。本轮只完成 profile，没有应用优化；M3 现场主要等待仍在分区提交/连接关闭，不能外推 NVMe 实验的倍数为线上提速。

结果：`docs/dev/PLAN-092-model-unknown-live-import-profile-OUTCOME.md`；原始数据：`docs/tests/20261021-23-WORKTREE/replay-month-schema-nvme.json`、`replay-month-schema-batch.json`。

2026-10-21 23:57 行号补正（同一更新者、同一版本）：上文 `_BufferedWriter` 类定义位于 `iosql/engines/sqlite/table.py:256`，`@guard_query` 位于 `:255`；`append()` 仍为 `:268`。补正不改变行为及计时结论。

## 2026-10-23：PLAN-093 修复

协作操作在对象生命周期内缓存稳定的 Resource 身份，URI 变化时重新解析；同线程仍复用现有 lease，generation/namespace_revision 检查和迁移互斥保持有效。没有删除 buffered_writer 的协作包装。

临时库中逐行调用 append 1,000 次并 flush，已打开表的资源路径重解析为 **0 次**，最终 1,000 行全部可见。`test_buffered_append_reuses_resource_identity` 与迁移屏障测试覆盖该路径；原始计数见 `docs/tests/20261023-PLAN-093/verification.json`。本次未测业务 HDD 导入总耗时。
