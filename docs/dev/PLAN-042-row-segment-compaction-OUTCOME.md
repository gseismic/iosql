# PLAN-042 Row Segment Compaction 实施结果

## 结论

已完成 SQLite `Segment.rows(...)` 表的显式 compaction 能力。

新增 API：

```python
result = table.compact(partition=None, target_rows=None)
segments = table.segments(include_obsolete=True)
```

新增 CLI：

```bash
iosql compact -s DB -t TABLE
iosql segments -s DB -t TABLE --include-obsolete
```

本轮实现面向金融 K 线、tick、orderbook 等 append-only 大规模写入/查询场景：晚到数据或回补数据进入新 active/delta segment 后，可通过 `compact()` 把当前可见 segment 重新按 `order_key` / time / 主键排序并写入新的 sealed generation，减少 stats 范围重叠导致的额外扫描。

## 已完成

### manifest 状态

- `SegmentInfo` 新增 `state` 字段。
- 兼容旧库：没有 `state` 的旧 manifest 行按 `sealed` 推断为 `sealed` 或 `active`。
- `_upsert_segment_info()` 使用 `ensure_column=True` 自动补齐 manifest 的 `state` 列。
- `_segment_infos(..., include_obsolete=False)` 默认过滤 `obsolete`。
- `_physical_part_refs()`、查询、诊断、索引和重分区默认只使用非 obsolete segment。
- 新 segment id 基于包含 obsolete 的历史最大 id 递增，避免 manifest 主键复用。

### compact API

- `Table.compact(partition=None, target_rows=None)`：
  - 仅支持有 `TableContract` 且启用 `Segment.rows(...)` 的 SQLite 表；
  - `partition=None` 时整理所有 logical partition；
  - 默认使用 `spec.physical_layout.target_rows_per_file`；
  - 读取当前可见 active/sealed segment；
  - 按 `order_key`、time、partition column、primary key 排序；
  - 按目标行数切分新 segment；
  - 写入新 sealed generation；
  - 成功后将旧 segment 标记为 `obsolete`。
- 返回 `CompactResult`，包含 rows、old/new segment 和 obsolete 统计。
- compact 失败时会尽量把已写入的新 segment 标记为 obsolete，不会先隐藏旧 segment。

### append-only 主键语义

`Segment.rows(...)` 仍保持 append-first 语义，本轮没有开放 `upsert/replace`。

为避免 compact 破坏已有合法数据，重写时如果同一 chunk 内即将出现重复主键，会提前切到下一个新 segment。这样跨旧 segment 的重复主键仍然跨新 segment 保存，查询结果保持不变。

### CLI

- `iosql compact` 输出 `CompactResult` JSON。
- `iosql segments --include-obsolete` 可审计旧 segment 状态。
- `--partition` 支持 JSON 解析，数值分区可传 `--partition 0`。

### 文档

已更新：

- `README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/api-reference.md`
- `skills/iosql-usage/references/usage-patterns.md`
- `skills/iosql-usage/references/troubleshooting.md`

## 测试

已运行：

```bash
python -m pytest tests/test_cli.py tests/engines/sqlite/test_partitioned_table_contract.py tests/engines/sqlite/test_timeseries_contract.py -q
```

结果：

```text
82 passed in 12.02s
```

已运行：

```bash
python -m compileall iosql tests -q
python -m pytest tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/core/query tests/utils/mysqlite -q
```

结果：

```text
228 passed, 12 skipped in 20.29s
```

已运行：

```bash
python -m pytest tests/test_partition_integration.py -q -k 'not test_upsert_large_data'
```

结果：

```text
41 passed, 1 deselected in 13.81s
```

全量测试尝试：

```bash
python -m pytest tests -q
```

结果：

```text
220 passed, 13 skipped in 358.74s
KeyboardInterrupt
```

中断原因：超过 5 分钟仍处于仓库历史已知的长耗时大数据用例阶段，没有失败输出。已单独补跑 `tests/test_partition_integration.py -k 'not test_upsert_large_data'`，除该超长用例外通过。

## 保留边界

1. `compact()` 不物理删除 obsolete SQLite 文件；默认查询已不使用它们。
2. `compact()` 不做主键去重，不实现 tombstone。
3. `Segment.rows(...)` 仍不支持 `upsert/replace`。
4. 当前没有后台自动 compact，需要用户或 CLI 显式触发。
5. concurrent writer 与 compact 仍建议由调用方串行化；主 API 对分区 LRU 是线程安全的，但 compaction 是布局维护操作，不应与同表写入并发运行。

## 下一步建议

下一步高价值目标应是 **PLAN-043：segment obsolete 文件安全清理与 deep validate**。

理由：

1. 本轮已经产生 `obsolete` 状态，但为安全起见没有物理删除旧文件。
2. 生产运维需要确认 manifest、物理文件、row_count、stats 和 SQLite integrity 一致。
3. 在没有 deep validate 和安全清理前，继续做 tombstone/upsert 会扩大不可见数据和旧文件管理复杂度。
4. CLI 设计里曾明确留下 `iosql validate --deep`，现在有了 obsolete/generation 后，它的价值更高。

建议 PLAN-043 覆盖：

- `iosql validate --deep`：校验 manifest、文件存在性、row_count、stats、primary key index、obsolete 引用和 SQLite integrity。
- `table.cleanup_obsolete(...)` / `iosql cleanup-obsolete`：只在 validate 通过、无活跃租约时删除 obsolete 文件。
- dry-run 默认开启，真实删除必须显式 `--yes`。
