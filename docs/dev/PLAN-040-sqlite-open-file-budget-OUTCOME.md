# PLAN-040 SQLite 打开文件预算实施结果

## 结论

已完成 SQLite 多分区文件描述符预算的进程内控制。主 API 不再要求调用方通过
提高 `ulimit -n` 规避 `Too many open files`，默认资源策略为：

```python
Database(
    "sqlite://data/market.iosql",
    max_open_partitions=5,
    max_connections_per_file=1,
)
```

## 根因

本次定位到三个独立问题：

1. mysqlite 的 `_active_databases` 使用强引用 `set`。高层数据库即使失去业务引用，
   低层数据库仍被清理注册表永久保活。修复前连续遗弃 30 个实例，FD 增量为 270，
   且 `gc.collect()` 无法释放。
2. 分区缓存上限固定在私有字段 `_num_opened_max_allowed=5`，调用方没有稳定 API。
3. 首次创建分区时先创建缓存数据库，再创建一个临时建表数据库；同一 SQLite 文件
   被重复连接，并重复执行 WAL PRAGMA。

批量插入还存在一个隐藏的双连接依赖：已经持有写入 cursor 时，
`default_values()` 再次从同一连接池借连接。连接池上限为 1 时会等待自身。

## 实施内容

### 高层资源上限

- `Database` 增加 `max_open_partitions`，默认 `5`。
- `Database` 增加 `max_connections_per_file`，默认 `1`。
- 两个参数必须是正整数，只传递给 SQLite 引擎。
- SQLite `Table` 提供同名只读属性。
- 元数据库、表元数据库和所有分区数据库统一使用单文件连接上限。

### 分区连接复用

- 分区数据库继续使用 `OrderedDict` LRU 淘汰。
- 建表和读取分区行不再创建临时 `MyDatabase`，统一复用 LRU 缓存。
- 单个活跃逻辑表的分区数据库数量不超过 `max_open_partitions`。

### 泄漏修复

- `_active_databases` 改为 `weakref.WeakSet`。
- 全局退出清理仍可处理活跃实例，但不再延长已遗弃实例的生命周期。
- 显式 `close()` 和上下文管理器仍是确定性释放资源的标准方式。

### 单连接兼容

- mysqlite 批量插入在读取 table info/default values 时复用当前 cursor。
- `max_connections=1` 可以完成自动建表、分批插入和事务提交，不再嵌套借用。

### 压力工具

新增 `benchmarks/stress_open_files.py`，覆盖：

- 大量逻辑分区写入；
- 全分区扫描；
- 反复创建并遗弃数据库实例；
- `/proc/self/fd` 或 `/dev/fd` 实时峰值采样；
- `RLIMIT_NOFILE`、分区缓存峰值和连接峰值报告。

## 本地验证

### 泄漏复现对比

修复前，连续创建并遗弃 30 个数据库：

```text
5 -> +45 FD
10 -> +90 FD
20 -> +180 FD
30 -> +270 FD
gc.collect() 后仍为 +270 FD
```

修复后相同测试：

```text
5/10/20/30 个实例的 FD 增量均为 0
```

### 中等规模压力

参数：

```text
partitions=100
abandoned_instances=50
max_open_partitions=5
max_connections_per_file=1
```

结果：

```text
fd_baseline=4
fd_peak=34
fd_final=4
peak_open_partitions=5
peak_connections=6
elapsed_seconds=1.945297
```

### 自动测试

```text
针对性测试：14 passed
ulimit -n 256：262 passed, 14 skipped, 1 deselected
```

被排除的遗留用例 `test_upsert_large_data` 会创建 1 万个日分区，并进行全扫描和
全量重写。本机运行时 FD 稳定在约 28 至 30，但 WAL LRU 关闭会执行 checkpoint
和 fsync，导致该反模式布局耗时超过 7 分钟。该用例将在提交后于 Linux 环境继续
验证；它的耗时不属于 FD 泄漏。

## 文档

- README 增加主 API 资源参数、WAL FD 粗略预算和压力命令。
- skill 主指南增加默认上限和生命周期规则。
- API reference 增加参数签名。
- troubleshooting 删除修改私有字段和依赖 `ulimit` 的建议。
- write performance 增加文件描述符预算章节。

## 后续验证

提交后在 `/media/lsl/G1`、默认 `ulimit -n=1024` 下执行：

```bash
python benchmarks/stress_open_files.py \
  --root /media/lsl/G1/iosql-fd-stress \
  --partitions 500 \
  --abandoned-instances 150 \
  --max-open-partitions 5 \
  --max-connections-per-file 1
```

并在相同默认限制下运行 Linux 全量测试，结果将在 G1 验证提交中补充。
