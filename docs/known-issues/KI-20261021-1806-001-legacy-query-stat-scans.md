# KI-20261021-1806-001：查询前重复统计拖慢导入检查

- 创建：2026-10-21 18:06（Asia/Shanghai）。模型：Codex，精确模型 ID 未提供，记为 `model-unknown`。
- 初始状态：未解决。
- 来源：用户要求研究 SQLite 写入慢，并指定 `projects/crypto.bn_data_sync` 的 CSV 导入场景。
- 确认版本：`5f6ff5b17ae9e23948d3f3697bf411c1b555c999`；引入提交未追溯。旧安装版本 `b752db62fa34c31a880d4bf8b3cdcdce011ea48c` 也复现；该目录随后由用户删除。

## 触发和影响

无 row segment 的分区表执行带 where 的查询时，`iosql/engines/sqlite/table.py:3102` 会进入非 lightweight 的 `_legacy_segment_info()`（同文件 `:2951`），在实际条件 SQL 之前读取时间范围、全列 MIN/MAX 和 COUNT。统计不是直接利用查询所需的主键索引。列统计可命中进程缓存，但时间范围和 COUNT 仍执行，列统计冷缓存或缓存键变化时还会再次扫描全列。

业务 `export_to_iosql.py:141` 为确认 CSV 全部主键存在而查询 symbol 和时间范围，30 天需访问 120 个六小时分区。现场日志中，全部数据已存在的月份也花约 100～175 秒才报告跳过。

## 确认证据

以只读 SQLite backup 创建 4 个真实分区的临时副本，保留全部 9,929 条分区登记；使用当前源码和原导入函数，1,440 行、重复 3 次：完整性检查中位数 `0.6762s`，实际写入 `0.0524s`。仅在诊断进程让 legacy 引用采用 lightweight 后，检查降至 `0.0346s`，整次导入从 `0.7624s` 降至 `0.1375s`；新主键、已存在主键及物理行数核验通过。实验没有应用到正式代码。

当前没有发布的修复。候选方向是普通查询仅做元数据寻址并执行原条件 SQL，避免为获取可选裁剪信息先扫描数据；保留全主键覆盖校验、缺文件错误、where 条件和时间分区裁剪。不能把“检查耗时高”处理为直接跳过数据完整性检查。

研究结果：`docs/dev/PLAN-090-model-unknown-sqlite-write-research-OUTCOME.md`；原始产物：`docs/tests/20261021-17-WORKTREE/kline-current-clone.json`。

## 2026-10-21 19:30：已验证修复

- 更新者：Codex，精确模型 ID 未提供，记为 `model-unknown`。
- 状态：未解决 → 已解决。用户明确授权后，按 PLAN-091 完成实施与验证。
- 修复版本：WORKTREE，基准 `5f6ff5b17ae9e23948d3f3697bf411c1b555c999`，尚未提交；修改 `iosql/engines/sqlite/table.py` 及相关回归测试、技能参考文档。
- 普通查询、query/latest、迭代和条件删除采用 lightweight legacy 引用，不再临时扫描 MIN/MAX、session 范围和 COUNT；显式 count 只计数。原始 SQL 条件和时间分区裁剪保留，已有 row Segment 统计继续使用，显式诊断仍可计算统计，explain 的裁剪决定与普通查询一致。
- 缺失的已登记 legacy 文件保留引用并报错，避免读取成空表或被索引/补列操作重建。正常 Segment-only 布局不要求存在同名 legacy 文件。
- 专项 27 项通过，最终 SQLite/Parquet/partition 回归 271 项通过。扩展回归中的 6 项既有失败在原 HEAD 独立目录复现，另一个旧裁剪断言已更新并通过；连接池偶发迭代异常另记 `KI-20261021-1930-001`。
- 真实 CSV/分区副本 3 轮复测：检查中位数 `0.6762 → 0.0308 秒`，完整导入 `0.7624 → 0.1031 秒`；没有替换查询路径。新主键写入、物理行数、全部主键覆盖、重复导入跳过均通过；普通路径统计 helper 的调用次数为 0。临时副本位于 NVMe，不外推为正式 HDD 的固定提速倍数。
- 未修改业务脚本和正式库，未重写显式诊断的统计缓存机制；这里解决的是普通访问前不必要的统计扫描。默认 Conda Python 已加载当前源码，新进程可直接使用。
- 结果：`docs/dev/PLAN-091-model-unknown-query-without-legacy-stat-scans-OUTCOME.md`；验证：`docs/tests/20261021-18-WORKTREE/README.md`。

## 2026-10-21 23:31：修复已提交

- 更新者：Codex，精确模型 ID 未提供，记为 `model-unknown`。
- 状态保持已解决；用户明确授权提交，实际修复提交为 `479f8e8eaeb13044aadd43a7bd5945cd4cc90694`。
- 该提交包含 PLAN-090 研究工具/记录、PLAN-091 引擎修复/回归/文档及问题索引；测试产物保留本地，没有推送。
- 本事件补充此前 WORKTREE 修复版本的实际提交依据，不改变验证结论或问题范围。
