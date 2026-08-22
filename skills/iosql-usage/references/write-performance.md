# iosql 大批量写入性能指南

## 适用负载

本文重点覆盖：

- 20 年股票日线历史回灌，约 2500 万行。
- orderbook、tick、逐笔等 append-only 高频数据。
- Linux ext4 + HDD 本地盘。

## 先测目标盘

不要用系统盘、SSD 或 tmpfs 的数字推断 HDD。仓库 benchmark 可直接写目标目录：

```bash
python benchmarks/benchmark_write.py daily \
  --root /media/lsl/G1/iosql-bench \
  --rows 25000000 \
  --batch-size 50000 \
  --profile balanced

python benchmarks/benchmark_write.py orderbook \
  --root /media/lsl/G1/iosql-bench \
  --rows 100000000 \
  --batch-size 100000 \
  --segment-rows 1000000 \
  --profile balanced
```

先用 100 万行 smoke，再跑完整规模。比较磁盘时保持 workload、rows、batch size、profile 完全相同。

推荐先自动扫描 G1 与 Z1：

```bash
python benchmarks/sweep_write.py daily \
  --root /media/lsl/G1/iosql-bench \
  --root /media/lsl/Z1/iosql-bench \
  --rows 2000000 \
  --batch-sizes 20000,50000,100000 \
  --profiles balanced,durable \
  --output /tmp/iosql-daily-disk-compare.json

python benchmarks/sweep_write.py orderbook \
  --root /media/lsl/G1/iosql-bench \
  --root /media/lsl/Z1/iosql-bench \
  --rows 5000000 \
  --batch-sizes 50000,100000,200000 \
  --profiles balanced,durable \
  --segment-rows 1000000 \
  --output /tmp/iosql-orderbook-disk-compare.json
```

扫描默认删除每个参数组合的临时数据库，只保留汇总 JSON 和 Markdown。使用 `--keep-runs` 才保留全部测试库。报告会记录：

- 文件系统、挂载参数和块设备 `rotational` 属性
- Python 与 SQLite 版本
- write rows/s、端到端 pipeline rows/s、近似 MiB/s
- 每个磁盘/profile 下的最佳 batch size

先用扫描结果选择参数，再对 G1 运行 2500 万行日线或更大 orderbook 完整测试。

## 推荐写入方式

### 日线历史回灌

```python
from iosql import Database

db = Database("sqlite:///media/lsl/G1/market.iosql")
daily = db.table(
    "daily",
    pk=["symbol", "date"],
    columns={
        "symbol": "text",
        "date": "text",
        "open": "real",
        "high": "real",
        "low": "real",
        "close": "real",
        "volume": "integer",
        "amount": "real",
    },
    partition="calendar(date,every=1year,timezone=Asia/Shanghai)",
    order_key="date",
    conflict="append",
)

for rows in source_batches:
    daily.write(rows)
```

建议：

- 单批先测 `50_000`，再比较 `20_000`、`100_000`。
- 按年分区通常比按日分区更适合日线：20 年约 20 个文件，而不是约 5000 个文件。
- 数据源按日期或分区排序后写入，减少同时打开和切换的 SQLite 文件。
- 历史回灌使用 append；需要修订时单独做按分区重写，不要把全量 upsert 当默认路径。

### orderbook / tick

```python
from iosql import Database, Segment

db = Database("sqlite:///media/lsl/G1/orderbook.iosql")
book = db.table(
    "orderbook",
    pk=["event_id"],
    columns={
        "event_id": "integer",
        "symbol": "text",
        "ts": "integer",
        "sequence": "integer",
        "side": "text",
        "price": "real",
        "size": "integer",
    },
    partition="duration(ts,every=1day)",
    segment=Segment.rows(
        max_rows=1_000_000,
        stats_columns=["ts", "sequence"],
    ),
    order_key="ts",
    conflict="append",
)

with book.write_buffer(max_rows=100_000) as buffer:
    for rows in stream_batches:
        buffer.append(rows)
```

建议：

- append-only 是高吞吐前提；`Segment.rows` 当前不支持高效跨 segment upsert。
- `write_buffer` 的 `max_rows` 是内存 flush 阈值，不是 SQLite 持久化 WAL。
- segment 可从 100 万行开始测；宽表可降低到 20 万至 50 万行，窄表可提高。
- 默认会为所有列维护 min/max。宽表只需要按时间/序列裁剪时，使用 `stats_columns` 限制统计列；未统计字段仍可查询，但查询计划会保守扫描。
- 写入顺序尽量与 `order_key` 一致，晚到数据会扩大 segment 范围并降低查询裁剪效率。
- 不要每条事件调用一次 `write()`；HDD 上应聚合到至少数千行，通常 5 万至 10 万行更合适。

## PRAGMA 与数据安全

iosql 默认 balanced 配置：

```text
journal_mode=WAL
synchronous=NORMAL
cache_size=-64000
temp_store=MEMORY
mmap_size=268435456
wal_autocheckpoint=1000
busy_timeout=30000
```

它适合大多数可重试抓取任务。需要更强的断电提交保障：

```python
db = Database(
    "sqlite:///media/lsl/G1/market.iosql",
    pragma_dict={
        "journal_mode": "wal",
        "synchronous": "full",
        "cache_size": "-64000",
        "temp_store": "memory",
        "busy_timeout": "30000",
    },
)
```

只有原始数据可确定重放时，才考虑：

```python
pragma_dict={"journal_mode": "wal", "synchronous": "off"}
```

`synchronous=OFF` 在掉电、内核崩溃或存储故障时可能损坏最近事务，不应作为默认生产配置。

## 文件描述符预算

主 API 默认使用：

```python
db = Database(
    "sqlite:///media/lsl/G1/market.iosql",
    max_open_partitions=5,
    max_connections_per_file=1,
    max_process_connections=32,
)
```

- `max_open_partitions` 限制每张表同时缓存的分区数据库数量，并以 LRU 淘汰。
- `max_connections_per_file` 限制每个 SQLite 文件的连接数。
- `max_process_connections` 限制进程内所有数据库和表的真实 SQLite 连接总数；预算
  用尽时会回收其他池的空闲连接，默认 `32`。
- WAL 模式下每条连接可能同时打开数据库、`-wal`、`-shm`，单表可按
  `(max_open_partitions + 2) * max_connections_per_file * 3` 粗估 FD 上限。
- 多线程读取确实需要同文件并发时，可将 `max_connections_per_file` 调到 `2`，
  但应先在默认系统 FD 限制下运行压力测试。
- 不要以提高 `ulimit -n` 掩盖连接泄漏；长期进程仍需显式 `close()`。

目标盘验证命令：

```bash
python benchmarks/stress_open_files.py \
  --root /media/lsl/G1/iosql-fd-stress \
  --partitions 500 \
  --abandoned-instances 150 \
  --max-open-partitions 5 \
  --max-connections-per-file 1 \
  --max-process-connections 32
```

G1 权威验证（2026-06-22，提交 `6326a4d`）：

| 项目 | 结果 |
|------|------|
| 设备 | `/dev/sdb1`，ext4，USB HDD `Elements 25A1` |
| Linux / SQLite | `6.8.0-52-generic` / `3.46.0` |
| FD 软限制 | `1024`，测试未提高 |
| 分区 / 遗弃实例 | `5000` / `500` |
| 进程 FD | baseline `4`，peak `41`，final `4` |
| 分区 / 连接峰值 | `5` / `6` |
| 压测耗时 | `95.720033s` |
| Linux 全量测试 | `263 passed, 14 skipped` |

该结果验证的是文件描述符预算和回收，不代表日线或 orderbook 的 rows/s。

PLAN-041 并发与进程预算复验（2026-06-23）：

| 项目 | 结果 |
|------|------|
| 进程预算 | `16` |
| 分区 / 遗弃实例 | `5000` / `500` |
| 进程连接峰值 | `15` |
| 进程 FD | baseline `4`，peak `48`，final `4` |
| 分区 / 单表连接峰值 | `5` / `6` |
| 压测耗时 | `63.559715s` |
| Linux 全量测试 | `278 passed, 14 skipped` |

## ext4 HDD 与 NTFS

- Linux 本地 ext4 通常更适合 SQLite 的 WAL、锁和 fsync 语义。
- NTFS 的实际性能取决于驱动、挂载参数和写缓存，不能只凭文件系统名称下结论。
- 在同一机器上用相同 benchmark 比较 `/media/lsl/G1` 与 `/media/lsl/Z1`。
- 不要把数据库放在网络文件系统或不可靠的用户态挂载上执行多进程写入。
- 单个逻辑表保持单 writer；SQLite 可以并发读，但多个 writer 最终仍会串行争用写锁。

## 诊断顺序

1. 确认 `batch_size`，每条写入通常是首要问题。
2. 确认 workload 使用 append 而不是 upsert/replace。
3. 确认分区数量不会产生大量小文件。
4. 查看 benchmark 的 rows/s、MiB/s、数据库大小。
5. 检查报告中的 `filesystem`、`mount_options`、`block_device.rotational`，确认实际测到目标盘。
6. 用 `iostat -xz 1` 观察磁盘 `%util`、`await`、吞吐。
7. 若 `%util` 接近 100% 且 CPU 空闲，瓶颈是 HDD；继续增加 Python 并发不会提高单盘吞吐。
8. 若 CPU 单核满但磁盘不忙，再用 `cProfile` 检查数据转换和分区计算。

## 反模式

- 每行调用一次 `write()`。
- 日线按天创建约 5000 个小分区文件。
- orderbook 默认 upsert。
- 每批写入后主动调用 `layout_report()`、`segments()` 或全量 count。
- 在同一 HDD 上并行写多个 iosql 表，造成随机寻道。
- 未记录 durability profile 就比较 benchmark。
