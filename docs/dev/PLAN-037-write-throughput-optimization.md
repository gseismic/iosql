# PLAN-037-write-throughput-optimization

## 背景

本轮目标是优化 iosql 在 Linux ext4 + HDD 上的大批量写入性能，重点覆盖两类负载：

1. 股票日线历史回灌：约 20 年、每个交易日 5000 支股票，数量级约 2500 万行。
2. orderbook 持续写入：高频、append-only、大批次、按 row segment 滚动。

当前开发机无法访问用户的 `/media/lsl/G1`（ext4）和 `/media/lsl/Z1`（NTFS），因此仓库需要提供可在目标 Linux 主机直接运行的 benchmark。开发机测试只用于比较优化前后的相对变化，不替代目标盘结果。

## 已确认瓶颈

基于 20000 行、每批 500 行的 `cProfile`：

- `write()` 和 `insert_all()` 重复读取分区、表元数据并重复生成 `WriteResult`。
- `_layout_warnings_for_parts()` 在每次写入后通过 `_legacy_segment_info()` 对数据文件执行 `COUNT/MIN/MAX`，普通表写入中约占总耗时 36% 至 45%。
- segment 每批写入后 `_refresh_segment_info()` 重扫整个 segment 的行数和所有列 `MIN/MAX`，持续写入会退化为 O(n²) 累计扫描。
- `getarg_diclist_insertmany()` 每批扫描全部行推断字段类型、复制每个 dict；固定契约表不需要重复推断。
- 高层 SQLite Table 未显式继承 mysqlite 默认 PRAGMA，数据文件虽然因首次建表进入 WAL，但仍使用 `synchronous=FULL`，元数据库还可能保持 rollback journal。

## 设计

### 1. 写入结果只计算一次

`write()` 完成 mode/contract 归一化后直接返回 `insert_all()` 的结果，不再在外层重复读取：

- `table_parts()`
- `get_table_meta()`
- layout warning

### 2. 写入热路径不做全表诊断

- 未配置 `Segment.rows(...)` 的表，写入结果不自动扫描物理文件生成布局警告。
- 配置 segment 的表只读取 manifest 中的 `row_count` 判断警告。
- 深度布局检查继续由显式 `layout_report()` 承担。

### 3. segment 统计改为增量维护

append-only 写入成功后：

- `row_count += len(batch)`
- 将本批次每列 min/max 合并到 manifest 现有 stats
- 从通用 stats 更新 time/session 范围
- 达到 `max_rows` 后 seal

`ignore/replace` 等无法从输入行数准确推导结果的路径保留全量刷新，确保正确性。

### 4. 固定 schema 批量参数快速转换

当批次字段是已存在表字段的子集时：

- 使用缓存的 `PRAGMA table_info` 字段顺序
- 直接生成参数矩阵
- 不再重复做字段 union 和类型推断

发现新字段时回退旧路径，以保留 `schema_policy=evolve`。

### 5. SQLite 写入配置

高层 SQLite 默认使用 balanced 配置：

```text
journal_mode=WAL
synchronous=NORMAL
cache_size=-64000
temp_store=MEMORY
mmap_size=268435456
wal_autocheckpoint=1000
busy_timeout=30000
```

`Database(..., pragma_dict=...)` 可覆盖配置：

- 强断电保障：`synchronous=FULL`
- 可重放原始数据的极限导入：`synchronous=OFF`，必须在文档中明确风险

### 6. benchmark

新增 `benchmarks/benchmark_write.py`：

- `daily`：模拟日线 schema 和分区写入
- `orderbook`：模拟 append-only orderbook 和 segment
- 可指定目标目录、总行数、batch size、segment rows、PRAGMA profile
- 输出 elapsed、rows/s、MiB/s、数据库大小
- 记录文件系统、挂载参数、块设备旋转属性、Python/SQLite 版本

新增 `benchmarks/sweep_write.py`：

- 自动扫描多个 batch size 和 durability profile
- 可在同一轮比较 `/media/lsl/G1` 与 `/media/lsl/Z1`
- 每个组合结束后可清理测试库，避免参数扫描占满磁盘
- 汇总生成 JSON 和 Markdown 排名，便于选择完整规模测试参数

目标盘命令示例：

```bash
python benchmarks/benchmark_write.py daily \
  --root /media/lsl/G1/iosql-bench \
  --rows 25000000 \
  --batch-size 50000 \
  --profile balanced
```

## 验证

1. 现有 SQLite、partition、mysqlite 测试全部通过。
2. 新增测试验证：
   - `write()` 不重复生成结果。
   - 普通写入不触发全文件 stats 扫描。
   - segment 增量统计与查询裁剪正确。
   - PRAGMA 默认值和覆盖值正确生效。
   - 固定 schema 快速路径保留缺省值和 schema evolve 行为。
3. 优化前后使用同一 benchmark 参数复测，记录吞吐提升和 profile 热点变化。
4. 在 README 和 `skills/iosql-usage` 中写明 ext4/HDD 最佳实践、批次大小、segment 选择和 durability 取舍。
