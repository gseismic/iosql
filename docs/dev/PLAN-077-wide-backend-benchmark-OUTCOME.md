# PLAN-077 实施结果：SQLite 与 Parquet 200 列宽表基准

## 交付内容

- 新增 `benchmarks/benchmark_wide_backends.py`，可配置行数列表、重复次数、写入批次、segment 上限、读取批次、临时根目录和报告文件。
- 表固定为 200 列：整数主键 `id` 加 199 个浮点列。SQLite 与 Parquet 使用相同的 iosql 表契约、确定性数据生成器和写入/读取 API。
- 对比写入吞吐与端到端吞吐，并分别测量 `count_rows()`、全 200 列流式读取、4 列投影读取和小范围条件读取。结果验证实际行数及首尾样本值，并记录 segment 裁剪计划。
- JSON 保存每轮原始耗时、吞吐、文件大小和环境；Markdown 给出中位数、读取耗时、吞吐和 Parquet 相对 SQLite 倍率。
- 本次结果保存在 [wide-backends-20261002.md](../../benchmarks/results/wide-backends-20261002.md) 和 [wide-backends-20261002.json](../../benchmarks/results/wide-backends-20261002.json)。

## 实测配置

- 行数：1,000、10,000、50,000、100,000；每个后端/规模重复 2 次。
- 写入批次与 `Segment.rows` 上限：均为 5,000 行；读取批次 1,000 行。
- 条件查询选择最后最多 1,000 行或总行数的 10%，投影 `id` 和 3 个数值列；使用 `id` segment stats 检查裁剪。
- 环境：Linux 6.8、Python 3.12.8、SQLite 3.46.0、DuckDB 1.3.2、12 个逻辑核心、ext4 上的 Kingston NVMe SSD。
- 完成 16 轮正式运行，所有写入/扫描/过滤计数和首尾抽样校验通过；默认清理临时数据库。

## 结果摘要

100,000 行时的两次运行中位数：

| 操作 | SQLite | Parquet | 对比 |
|---|---:|---:|---|
| 写入吞吐 | 3,689 行/秒 | 446 行/秒 | SQLite 约快 8.27 倍 |
| 全部 200 列读取 | 25.89 秒 | 18.29 秒 | Parquet 约快 1.42 倍 |
| 4 列投影读取 | 0.245 秒 | 0.409 秒 | SQLite 约快 1.67 倍 |
| 过滤读取 1,000 行 | 0.016 秒 | 0.031 秒 | SQLite 约快 1.93 倍 |
| 数据文件体积 | 206.19 MB | 105.79 MB | Parquet 约为 SQLite 的 51% |

全列扫描中 Parquet 在四档规模均快约 1.29～1.42 倍。当前 API 下，4 列投影和小结果集过滤在 SQLite 上更快；100,000 行过滤查询两边都只扫描 20 个 segment 中的 1 个。写入差距稳定，Parquet 约为 SQLite 写入吞吐的 12%。完整分档数据和中位数见 Markdown 报告。

## 口径与限制

- 写入 API 吞吐只累计 `Table.write()` 调用时间；端到端吞吐包含 200 列 Python 行对象生成及表初始化。
- 查询用 `iter_select_batches()` 并计时到结果行构造完成。单个 Parquet segment 的连接/扫描成本包含在耗时中；无 `ORDER BY`，不代表排序查询。
- `count_rows()` 后先测全列扫描，再测投影和过滤查询；操作顺序可能预热系统页缓存，因此结果是本机当前顺序下的热/温缓存表现，不是冷启动测试。
- 数据是有确定性重复模式的合成数值。压缩体积和真实业务数据分布有关，不能直接外推到任意内容。
- 这是当前 NVMe、DuckDB/SQLite 版本与 5,000 行 segment 设置下的对比；更换磁盘、segment 大小或业务 schema 后应重新运行脚本。

## Review 与验证

- Review 时发现 SQLite 在数据库关闭前可能把 segment 数据留在 WAL，直接统计 `.sqlite3` 文件会低估小表体积。现已在关闭数据库后统计数据文件；1,000 行 SQLite 数据文件体积经两次补测均为 2,093,056 字节。
- 小规模 smoke：`--rows 10,200 --batch-size 100 --segment-rows 100`，两后端均完成 200 列建表、写入、查询、segment 计划和 JSON/Markdown 输出。
- 正式基准：`--rows 1000,10000,50000,100000 --repeats 2 --batch-size 5000 --read-batch-size 1000`，共 16 轮。
- `python -m py_compile benchmarks/benchmark_wide_backends.py`、`ruff check benchmarks/benchmark_wide_backends.py`、`git diff --check` 均通过。
- 未修改 SQLite/Parquet 后端实现，也未触碰工作区中与本任务无关的既有未提交变更。
