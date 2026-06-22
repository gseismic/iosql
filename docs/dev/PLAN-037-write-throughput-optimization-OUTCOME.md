# PLAN-037-write-throughput-optimization-OUTCOME

## 实施摘要

本轮完成 iosql SQLite 主写入链路的性能分析、优化、benchmark 和最佳实践文档。

主要改动：

1. `write()` 不再重复读取分区和元数据、重复生成 `WriteResult`。
2. 普通非 segment 表写入后不再自动执行全文件 `COUNT/MIN/MAX` 布局诊断。
3. append-only segment 使用输入批次增量维护 row_count 和列 min/max，不再每批重扫历史 segment。
4. 固定契约 schema 使用快速参数转换；完整同构行通过 `itemgetter` 构造 executemany 参数。
5. 契约已覆盖全部字段时，首次建表跳过逐值类型推断。
6. 修复 mysqlite 批量事务：内部子批不再各自 commit，一个 `insert_all()` 只在结尾提交一次。
7. 高层 SQLite 默认明确启用 balanced PRAGMA，并允许 `Database(..., pragma_dict=...)` 覆盖。
8. CalendarPartition 增加实例级有界 LRU；同一交易日的 5000 支股票只解析一次日期。
9. 新增日线和 orderbook benchmark，可直接在 `/media/lsl/G1` 或 `/media/lsl/Z1` 运行。

## 瓶颈证据

优化前使用 20000 行、每批 500 行的本机 `cProfile`：

| 场景 | 优化前吞吐 | 主要热点 |
|------|-----------:|---------|
| 普通单文件 | 14249 rows/s | layout warning 全表统计、重复结果诊断、字段 union |
| range partition | 9624 rows/s | layout warning 全表统计、字段 union、重复元数据查询 |
| row segment | 12276 rows/s | 每批 `_refresh_segment_info()` 全量 COUNT/MIN/MAX |

profile 结论：

- 普通写入中，自动 layout warning 的文件扫描约占 36% 至 45%。
- 固定 schema 仍逐批 union 字段和推断类型，约占 25% 至 32%。
- segment 每批重扫历史数据，累计复杂度会随 segment 增长退化为 O(n²)。
- mysqlite 的内部 10000 行子批各自触发 auto commit；100000 行首批 profile 观察到 25 次 commit。
- CalendarPartition 对同一日期重复调用 `arrow.get()`；20000 次相同日期分区计算耗时约 7.2 秒。

## 优化后结果

### 相同 20000 行小基准

| 场景 | 优化前 | 优化后 | 提升 |
|------|-------:|-------:|-----:|
| 普通单文件 | 14249 rows/s | 143681 rows/s | 10.1x |
| range partition | 9624 rows/s | 121903 rows/s | 12.7x |
| row segment | 12276 rows/s | 89449 rows/s | 7.3x |

CalendarPartition 20000 次相同日期计算从约 7.2 秒降到约 0.03 秒。

### 1000000 行目标负载模拟

开发机为 macOS/APFS，不是用户目标 ext4 HDD，以下仅用于代码版本间相对比较：

| workload | batch | 配置 | write rows/s | pipeline rows/s |
|----------|------:|------|-------------:|----------------:|
| 日线 | 50000 | 按年 calendar partition，复合主键 | 75607 | 68150 |
| orderbook | 100000 | 按日 duration + 500000 row segment | 134316 | 115583 |

日线在修复内部子批重复 commit 前约为 30573 rows/s，修复后约 75607 rows/s，提升约 2.47x。

orderbook 在同一修复前约为 96037 rows/s，修复后约 134316 rows/s，提升约 1.40x。

## 正确性保障

新增测试覆盖：

- 普通写入热路径不调用全文件 stats。
- segment append 不调用全量 refresh，row_count/min/max 仍正确。
- 默认 `WAL + synchronous=NORMAL` 和 `synchronous=FULL` 覆盖配置。
- 固定字段快速参数转换，包括字段顺序不同和默认值。
- mysqlite 多子批是单一原子事务；后续子批失败时前面子批全部回滚。
- CalendarPartition 重复值命中实例级缓存。

混合不可比较类型会把对应 segment stats 标记为不完整，查询裁剪保守扫描，避免错误跳过数据。

验证结果：

```text
完整套件（补最后一个专项测试前）：
247 passed, 14 skipped in 283.42s

最终工作树，排除 3 个历史 TestLargeData：
249 passed, 14 skipped, 3 deselected in 10.03s

最终 3 个历史 TestLargeData：
3 passed in 299.83s

最终工作树合计：
252 passed, 14 skipped
```

同时通过：

- `python -m compileall iosql benchmarks -q`
- `git diff --check`
- balanced / durable / replayable 三种 benchmark profile smoke
- benchmark/sweep 工具与写入专项：`46 passed`

## 新增 benchmark

日线完整规模：

```bash
python benchmarks/benchmark_write.py daily \
  --root /media/lsl/G1/iosql-bench \
  --rows 25000000 \
  --batch-size 50000 \
  --profile balanced
```

orderbook：

```bash
python benchmarks/benchmark_write.py orderbook \
  --root /media/lsl/G1/iosql-bench \
  --rows 100000000 \
  --batch-size 100000 \
  --segment-rows 1000000 \
  --profile balanced
```

脚本默认保留数据库和 `report.json`。加 `--cleanup` 才删除结果。

报告同时输出：

- 文件系统类型
- 挂载源、挂载参数、块设备旋转属性
- Python 和 SQLite 版本
- write 调用次数
- write elapsed / total elapsed
- write rows/s / pipeline rows/s
- 数据库大小
- 近似写入 MiB/s
- PRAGMA profile

目标盘参数扫描：

```bash
python benchmarks/sweep_write.py daily \
  --root /media/lsl/G1/iosql-bench \
  --root /media/lsl/Z1/iosql-bench \
  --rows 2000000 \
  --batch-sizes 20000,50000,100000 \
  --profiles balanced,durable \
  --output /tmp/iosql-daily-disk-compare.json
```

扫描按相同 profile/batch 依次测试不同根目录，默认清理每个组合的临时库，最终保留 JSON 和 Markdown 排名。

## ext4 HDD 建议

- 日线从 `batch_size=50000` 开始，比较 20000、50000、100000。
- 20 年日线优先按年分区，避免按日生成约 5000 个小文件。
- orderbook 使用 append-only，单批 50000 至 100000 行，segment 从 100 万行开始测。
- 单 HDD 保持单 writer，多个并行写任务会增加随机寻道。
- 默认 balanced 使用 `WAL + NORMAL`。
- 不能重放的数据使用 `synchronous=FULL`。
- 只有抓取源可确定重放时才测试 `synchronous=OFF`。
- 用 `iostat -xz 1` 判断是磁盘 `%util/await` 还是 CPU 成为瓶颈。

## 文件

- `benchmarks/benchmark_write.py`
- `benchmarks/sweep_write.py`
- `README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/write-performance.md`
- `skills/iosql-usage/references/api-reference.md`
- `docs/dev/PLAN-037-write-throughput-optimization.md`
- `docs/dev/PLAN-037-write-throughput-optimization-OUTCOME.md`

## 环境限制

当前工作区是 macOS，`/media/lsl/G1` 和 `/media/lsl/Z1` 不存在，无法代替用户执行 ext4/NTFS 目标盘实测。仓库已提供相同参数 benchmark，最终绝对吞吐和两块盘的差异应以目标 Linux 主机生成的 `report.json` 为准。

为减少目标机上的人工反复，后续补充了 `sweep_write.py`：可同时比较 G1/Z1，自动扫描 batch size 和 balanced/durable profile，并汇总 JSON/Markdown 排名。单次报告同时记录挂载参数、块设备旋转属性以及 Python/SQLite 版本。
