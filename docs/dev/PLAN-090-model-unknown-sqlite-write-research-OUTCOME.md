# PLAN-090：SQLite 写入性能研究结果

- 完成：2026-10-21 18:10（Asia/Shanghai）。模型：Codex，精确模型 ID 未提供，记为 `model-unknown`。
- 当前引擎依据：`5f6ff5b17ae9e23948d3f3697bf411c1b555c999`，引擎源码未修改。
- 工作区新增诊断工具和研究记录，尚未提交。没有修改业务导入器、正式库数据或 Python 安装绑定。
- 计划：[PLAN-090](PLAN-090-model-unknown-sqlite-write-research.md)。

## 结论与优先级

用户的 CSV 导入主要慢在写入前的“整月主键是否已经全部存在”检查。这个检查通过 iosql 查询，但查询准备阶段先对六小时数据文件计算时间范围、全列 MIN/MAX 和 COUNT，读取同一文件中其他币种的数据；真正的 symbol + 时间查询已有覆盖索引。

当前源码的真实数据副本实验中，检查占整次导入约 89%；只在诊断进程省去这遍查询前统计，整次导入中位数从 `0.762s` 降到 `0.138s`，约 **5.5 倍**。这不是已经应用的优化，也不能直接外推为正式库整月导入的提速倍数。

建议实施顺序：

1. 普通 legacy 分区条件查询优先利用分区元数据与实际条件 SQL，避免为构建可选统计先扫描数据。保留全部主键覆盖检查；不能仅凭行数、首尾时间或文件存在就确认导入成功。对应 [KI-20261021-1806-001](../known-issues/KI-20261021-1806-001-legacy-query-stat-scans.md)。
2. 将固定结构批写的类型推断延迟到真正需要建表/补列时，再优化同构行参数转换。对应 [KI-20261021-1806-002](../known-issues/KI-20261021-1806-002-write-type-inference.md)。
3. 对业务已完整读入内存的 CSV，按现有 `batch_size` 切片后调用 `buf.append(batch)`，减少逐行进入缓冲 API 的次数。此项尚未单独量化，不承诺提速比例。
4. 前三项完成后再测机械盘上按时间分组、连接缓存和文件切换的收益。当前已经是 50,000 行缓冲批次，单纯继续增大 batch 或增加 writer 不能消除查询准备扫描。

## 真实业务与版本

业务目录：`/home/lsl/macbook/pai-studio-fin/projects/crypto.bn_data_sync`。以下业务代码路径相对于该目录。

| 项目 | 核实结果 |
| --- | --- |
| 业务版本 | 基准 `5abad0be27a60affbcedca63b8616ee0ff94b368`，含用户既有 WORKTREE 修改；本研究未改动该项目 |
| CSV 导入器 | `export_to_iosql.py` 无代码差异；SHA256 为 `de34cebbe5b7096b2af1f3b15fb57dada8983095e6ace75f834d1f898fa702a9` |
| 写入模型 | 13 列，主键 `symbol, open_time`，每批 50,000 行，六小时 duration 分区，无 row segment |
| 写入策略 | `export_to_iosql.py:201` 普通月份 ignore，重下载月份 replace；`:202` 创建缓冲器，`:203` 逐行 append |
| 入库前检查 | `export_to_iosql.py:141` 构造 CSV 时间戳集合；`:153` 调用带条件的 `iter_select(fields="open_time")` |
| 正式目标 | M3 下 `kline.sqlite-6h.iosql`，9,929 个分区；`/dev/sdc1`、ext4、USB 机械盘 Elements 10B8 |
| 原 CSV | Z1 下 `kline.csv/1m`，`/dev/sda3`、fuseblk；本轮未执行下载或正式导入 |

初查时默认 Conda Python 加载 `/home/lsl/library/iosql`，版本为 `b752db62fa34c31a880d4bf8b3cdcdce011ea48c`。用户随后删除该目录，明确以当前工作区为准；本报告优先使用当前源码复测。删除前的旧版本结果保留在原始产物中作历史对照。

删除后核验：默认 `/home/lsl/miniconda3/bin/python` 直接 `import iosql` 报 `ModuleNotFoundError`。本研究未重装包；复现程序通过 `--library` 显式使用当前源码。运行中的进程和其他 Python 环境未据此推断为相同状态。

## 为什么“跳过”也要两分钟

`log/iosql_export.log` 中，2026 年 ETHUSDT 的连续月份均报告“全部 CSV 主键已存在，跳过”，但相邻完成时间仍相差约 103～174 秒。13:06 启动的导入日志最后一次中断堆栈停在 `_legacy_segment_info → _data_file_time_range`，尚在导入前检查。

当前源码链路：

```text
export_to_iosql.py:153  month_already_imported / iter_select
  → iosql/engines/sqlite/table.py:3102  _get_select_part_refs
  → :2988  _physical_part_refs
  → :2951  _legacy_segment_info（带 where，未采用 lightweight）
  → :2536  _data_file_time_range
  → :2592  _data_file_column_stats（全部逻辑列）
  → :2521  _data_file_row_count
  → 原始 symbol + open_time 条件查询
```

列统计可以命中进程缓存，但首次查询或缓存键变化时会扫描全部列，时间范围和 COUNT 仍要读取文件。既有 Review 已指出 WAL 缓存失效问题，后续优化不能继续依赖仅 mtime/size 判断有效性。

只读检查正式库 2026-09-01 首个六小时文件：33,492,992 字节、268,754 行，只有复合主键索引。`EXPLAIN QUERY PLAN` 确认 symbol + open_time 查询使用 **COVERING INDEX**。

| 操作 | 墙钟时间 |
| --- | ---: |
| 时间 MIN/MAX | 1.0865 秒 |
| 全部 13 列 MIN/MAX | 0.1611 秒 |
| COUNT | 0.0028 秒 |
| 按 symbol + 时间查 360 行 | 0.000388 秒 |

这些查询依次执行，后续操作受前面读取预热，不能据此宣称同倍率的冷盘性能。正式库的数据测量使用 `mode=ro`、`query_only=ON`；元数据读取和 backup 的源连接也使用 `mode=ro`。

另一次只读实验直接在主键索引上校验 ETHUSDT 2026-09：120 个文件共约 4.12 GB，完整确认 CSV 的 43,200 个时间戳全部存在，耗时 **6.14 秒**，CPU 为 0.093 秒。这保留了全部主键覆盖判定，但不是原 API 的同缓存状态 A/B 测试，因此只作为方向验证。

## 真实 CSV 与隔离副本实验

完整解析 2026-09 CSV 的 43,200 行，3 次中位数 **0.158 秒**，解析不是现场两分钟级延迟的主要来源。

副本实验使用 SQLite 只读 backup 复制真实的 4 个分区（共约 135 MB），保留原来的全部 9,929 条分区登记，取 CSV 第一天 1,440 行。每轮用新的测试 symbol，在 `/tmp` 的 NVMe 副本执行原 `import_csv_files()`，每种方案重复 3 次。

实验方案仅在当前进程令 legacy 引用采用已有 lightweight 分支，SQL 的 symbol、时间条件与实际主键写入保持执行。每轮均验证实际写入 1,440 行，另外验证已有主键时检查返回 True；副本结束后清理。

**当前工作区源码结果**：

| 阶段 | 当前逻辑中位数 | 省去查询前统计的实验中位数 |
| --- | ---: | ---: |
| CSV 解析 | 0.0038 秒 | 0.0042 秒 |
| 入库前完整性检查 | 0.6762 秒 | 0.0346 秒 |
| 实际 `write()` | 0.0524 秒 | 0.0493 秒 |
| 整次导入 | 0.7624 秒 | 0.1375 秒 |

检查阶段约快 19.5 倍，整次约快 5.5 倍。不同阶段分别取中位数，不能要求各列中位数相加恰好等于总时间。

旧安装版本删除前的相同副本实验：检查 `0.6714 → 0.0350 秒`，整次 `0.7385 → 0.0906 秒`，约 8.2 倍。该数字仅是历史对照，当前结论使用上表。

旧版本的独立 profile 显示，完整性检查总计 0.743 秒，其中构造 legacy 引用 0.691 秒、全列统计 0.623 秒、真正的条件游标消费很小；当前源码复测同样复现该热点。

## 通用写入的第二个瓶颈

`iosql/utils/mysqlite/table.py:528` 即使无需自动建表或补列，也逐行逐列调用 `_union_fields_of_dictlist()`。新物理文件先写失败、建表重试时，会扫描同一批两次。

历史对比确认，`81c5e0ab8acdac39807f890a64d8306dd9640c9f` 修复表达式默认值时替换了原固定结构快速路径。默认值和异构行顺序的正确性必须保留，不能直接回退该修复。

Linux i5-12490F、ext4 NVMe、Python 3.12.8、SQLite 3.46.0；2 万行、5 千行/批、200 列非整数浮点数据，每种场景重复 3 次：

| 路径 | 写入行/秒中位数 |
| --- | ---: |
| 原生 sqlite3，单文件 | 58,882 |
| mysqlite，单文件 | 7,438 |
| iosql，单文件 | 5,971 |
| iosql，5 千行 segment，仅统计 id | 3,802 |
| 同上，进程内省去无用类型扫描 | 24,573 |
| 同上，再增加同构行快速转换 | 29,801 |
| 同上，当前逻辑统计全部 200 列 | 3,450 |

原生单文件对照预先建表，iosql 首次 write 包含物理建表和目录库语义；该对照用于拆分成本，不是功能等价的替代实现。数据生成、初始化、关闭、校验均单独记录，不计入写入 API 耗时。

10,000 行、2 个新 segment 的独立 profile：约 400 万次类型推断，字段合并 8.714 秒/总计 9.449 秒；`executemany` 为 0.068 秒。profiler 会放大 Python 调用成本，提速结论使用上表未启用 profiler 的结果。实验没有减少 `commit()` 调用次数或调整 WAL/NORMAL 设置。

其他对照：8 列逐行 `write()` 约 938 行/秒，批量明显更快；`Segment.rows + ignore` 对无重复输入仍逐行查重和维护 manifest，1,000 行实验约 671 行/秒。**实际业务没有启用 Segment，不能用这一结果解释当前业务。** 小 segment 与过细分区增加建文件和切换成本，但不会因此建议再次重分区正式库。

## 交付、验证和限制

- 新增 `benchmarks/diagnose_sqlite_write.py`：原生/mysqlite/iosql、8/200 列、批次、segment、统计列、FULL/NORMAL、ignore、profile 及进程内实验对照。
- 90 轮正式未启用 profiler 的通用实验完成，关闭后通过只读连接验证物理行数、主键和、首尾整行值；新增工具 14 场景 smoke 通过。
- 两个写入实验各检查默认值/NULL、异构 REPLACE 顺序、同构 key 顺序和批次失败回滚，共 8 项断言组通过，原方法恢复验证通过。这些不是正式优化已经完成全部回归的证明。
- 当前源码与旧安装各完成 6 次真实副本导入，均核实物理写入与存在性检查；正式库测试仅执行只读操作。
- `ruff check benchmarks/diagnose_sqlite_write.py`、`git diff --check` 通过。没有修改引擎，因此未运行包含既有失败项的整库回归。
- 原始数据、profile、复现程序和命令见 [测试记录](../tests/20261021-17-WORKTREE/README.md)。测试目录产物按规范不参与提交；研究报告保留必要摘要。
- 本轮不会清空系统页缓存，磁盘统计与副本实验不是严格冷缓存基准。只读研究可能预热系统缓存；用户的体感加速不能据此归因于已发布代码优化。
- 研究已经完成；两个性能问题仍未修复，状态保留为未解决。实际业务和引擎均未改动，未执行 commit/push。
