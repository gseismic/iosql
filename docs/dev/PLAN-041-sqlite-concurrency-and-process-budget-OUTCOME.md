# PLAN-041 SQLite 并发与进程级连接预算实施结果

## 结论

已修复 PLAN-040 复审发现的四项问题，并补充已有库继续写入和重分区的自包含示例。

主 API 的默认 SQLite 资源配置现在是：

```python
Database(
    "sqlite://data/market.iosql",
    max_open_partitions=5,
    max_connections_per_file=1,
    max_process_connections=32,
)
```

三层限制分别控制单表分区缓存、单文件连接池和进程真实 SQLite 连接总数。

## 修复内容

### 进程级连接预算

mysqlite 连接池新增共享预算管理器：

- 每条真实 `sqlite3.Connection` 创建前获取一个进程令牌；
- 关闭连接时严格归还令牌；
- 预算耗尽时跨连接池回收一个空闲连接；
- 多个活跃配置同时存在时使用最小预算；
- 连接池被垃圾回收时通过析构清理空闲连接和预算；
- 高层和低层 API 都可读取进程预算状态。

复审时在 `RLIMIT_NOFILE=128` 下，第 21 张活跃表会失败。修复后的自动测试使用
默认进程预算 32，同时持有 50 张表和另外 25 个 `Database` 实例，未触发
`EMFILE`；显式配置预算 16 的复现测试中连接数也始终不超过 16。

### 分区 LRU 并发安全

- 分区缓存增加 `Condition + RLock`。
- 每次分区操作通过上下文租约覆盖完整 SQL 操作。
- LRU 只淘汰租约为 0 的分区数据库。
- 达到缓存上限且所有分区正在使用时等待租约释放。
- `Table.close()` 等待分区租约结束，并阻止关闭期间创建新的分区池。
- schema 创建和 evolve 拆成独立租约阶段，避免跨分区嵌套等待。

复审前 20 线程扫描会稳定出现 `KeyError`，并发写入会出现
`connection pool is closed` 或死锁。修复后：

```text
12 线程 × 5 次全分区扫描：无异常，全部退出
4 线程并发写入：120/120 行完整，无异常，全部退出
```

### 连接池关闭竞态

原实现 checkout 在持有非重入锁时调用 `_decrement_created()`，会再次获取同一把锁。
现在关闭连接和计数更新都在锁外完成，稳定复现的 close/validation 竞态可以正常退出，
等待线程收到 `RuntimeError`，不再永久阻塞。

### repartition_to

- SQLite 目标继承源表的完整 PRAGMA。
- 目标继承 `max_open_partitions`、`max_connections_per_file` 和
  `max_process_connections`。
- PostgreSQL 等非 SQLite 目标不接收 SQLite 专属参数。
- 目标数据库使用 `try/finally`，写入异常时也会关闭。

## 自包含已有库示例

新增根目录和 skill 镜像：

- `09_existing_database_append.py`
- `10_existing_database_repartition.py`

默认模式下，每个脚本只准备自己的独立源库并关闭，然后通过新的 `Database` 实例按
持久化契约重新打开，分别演示增量写入和重分区。两个脚本互不依赖，也不依赖其他
example。

设置环境变量后可指向真实已有库：

```text
IOSQL_EXISTING_URI
IOSQL_EXISTING_TABLE
IOSQL_SOURCE_URI
IOSQL_TARGET_URI
```

使用外部源库时不会执行清理；重分区始终写新的目标 URI。

## 测试结果

### 本地

```text
SQLite/partition/mysqlite 重点回归：150 passed
修复复现与示例回归：23 passed
连接池专项：10 passed
RLIMIT_NOFILE=128：277 passed, 14 skipped, 1 deselected
```

`test_upsert_large_data` 在本机仍因创建、扫描和重写 1 万个 WAL 分区耗时过长而单独
交由 Linux 全量测试覆盖。

同机串行 50 万行日线基准：

```text
0fb0f79 基线：57,621 rows/s
PLAN-041 当前：55,623 rows/s
差异：-3.5%
```

该差异在单次 HDD/文件系统波动范围内，没有确认到显著写入性能回归。

### Linux 全量

环境：

```text
Linux 6.8.0-52-generic
SQLite 3.46.0
ulimit -n=1024
```

结果：

```text
278 passed, 14 skipped in 303.72s
```

全量测试包含 1 万分区遗留用例、新增并发测试、进程预算低 RLIMIT 子进程测试和
repartition 异常关闭测试，以及 4 个现有库追加/重分区自包含示例测试。

### G1 ext4 HDD

设备：

```text
/dev/sdb1
ext4 rw,relatime
USB HDD Elements 25A1
```

参数与结果：

```text
partitions=5000
abandoned_instances=500
max_open_partitions=5
max_connections_per_file=1
max_process_connections=16
peak_process_connections=15
peak_open_partitions=5
peak_connections=6
fd_baseline=4
fd_peak=48
fd_final=4
elapsed_seconds=63.559715
```

报告保留在：

```text
/media/lsl/G1/iosql-benchmark/20260623-plan041/fd-stress/
```

## 文档

- README 增加进程预算、并发 LRU 和已有库示例。
- skill 主指南、API reference、troubleshooting、usage patterns 和
  write-performance 同步更新。
- mysqlite README 和连接池清理文档增加进程预算 API。
- examples README 增加两个自包含已有库工作流。
