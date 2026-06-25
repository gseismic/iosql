# PLAN-044 segment 校验与 obsolete 安全清理 OUTCOME

## 结果

已完成 SQLite row segment 的深度校验和 obsolete 文件安全清理能力。

本轮把 `PLAN-042` 的 compaction 生命周期补齐为：

```text
写入 active/delta segment
-> compact 生成新 sealed generation
-> 旧 segment 标记 obsolete
-> validate 校验当前可见 segment
-> cleanup-obsolete 显式释放旧文件
```

这对金融海量数据的价值是：K 线、tick、逐笔、orderbook、新闻事件等 append-only 表在晚到数据和回补整理后，可以在可证明当前可见 segment 自洽的前提下释放磁盘空间，避免 compact 越用越占盘。

## 新增 API

### `table.validate(...)`

```python
validation = table.validate(deep=True, include_obsolete=False)
print(validation.ok, validation.errors, validation.warnings)
```

行为：

- `deep=False`：做 manifest/path 基础检查。
- `deep=True`：校验当前可见 segment 文件存在性、SQLite `PRAGMA integrity_check`、`data` 表、`row_count`、time/session 范围和 manifest stats。
- `include_obsolete=True`：同时检查 obsolete segment；obsolete 的深度问题以 warning 记录，不影响默认查询路径。

返回：

- `SegmentValidationResult`
- `ValidationIssue`

### `table.cleanup_obsolete(...)`

```python
preview = table.cleanup_obsolete()
cleanup = table.cleanup_obsolete(dry_run=False)
```

行为：

- 默认 `dry_run=True`，只预览候选文件和可释放字节数。
- `dry_run=False` 才删除文件。
- 删除前默认执行 `validate(deep=True, include_obsolete=False)`。
- 校验失败时抛出 `UnsafeOperation`，不会删除 obsolete 文件。
- 只删除 obsolete segment 的主 SQLite 文件及同名 `-wal`/`-shm` 文件。
- 删除成功后移除对应 `_iosql_segments` manifest 行。
- 遇到活跃租约或越界路径会跳过并写入 `skipped`。

返回：

- `CleanupResult`

## 新增 CLI

```bash
iosql validate -s DB -t TABLE --deep
iosql validate -s DB -t TABLE --deep --include-obsolete

iosql cleanup-obsolete -s DB -t TABLE
iosql cleanup-obsolete -s DB -t TABLE --yes
iosql cleanup-obsolete -s DB -t TABLE --partition PART --yes
```

CLI 规则：

- `cleanup-obsolete` 默认 dry-run。
- 只有 `--yes` 才执行删除。
- 删除前默认校验当前可见 segment。
- `--no-validate` 可跳过删除前校验，但不推荐用于生产。
- `validate` 发现 error 时返回退出码 `2`。

## 文件变更

### 核心

- `iosql/spec.py`
  - 新增 `ValidationIssue`
  - 新增 `SegmentValidationResult`
  - 新增 `CleanupResult`
- `iosql/__init__.py`
  - 导出新增结果类型。
- `iosql/engines/sqlite/table.py`
  - 新增 `validate(...)`
  - 新增 `cleanup_obsolete(...)`
  - 新增 SQLite integrity、row_count、time/session、stats 校验逻辑。
  - 新增 obsolete 文件删除、WAL/SHM 清理、manifest 行删除。
  - 新增删除前路径边界检查和活跃租约跳过。
- `iosql/cli.py`
  - 新增 `validate` 命令。
  - 新增 `cleanup-obsolete` 命令。

### 测试

- `tests/engines/sqlite/test_partitioned_table_contract.py`
  - compact 后 deep validate 通过。
  - cleanup 默认 dry-run 不删除文件。
  - cleanup 执行后删除 obsolete 文件和 manifest 行，查询结果不变。
  - 人为篡改 manifest row_count 后 deep validate 返回错误。
  - 当前可见 segment 校验失败时 cleanup 拒绝删除 obsolete 文件。
- `tests/test_cli.py`
  - help 输出新增命令。
  - CLI validate 输出 JSON。
  - CLI cleanup-obsolete 默认 dry-run。
  - CLI cleanup-obsolete `--yes` 执行删除并移除 obsolete manifest。

### 文档 / skill

- `README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/api-reference.md`
- `skills/iosql-usage/references/usage-patterns.md`
- `skills/iosql-usage/references/troubleshooting.md`

## 验证

已执行：

```bash
python -m compileall iosql tests -q
python -m pytest tests/engines/sqlite/test_partitioned_table_contract.py tests/engines/sqlite/test_timeseries_contract.py tests/test_cli.py tests/test_migration.py -q
python -m pytest tests/test_migration.py tests/test_cli.py tests/test_partition.py tests/test_partition_string.py tests/engines/sqlite tests/engines/postgresql tests/core/query tests/utils/mysqlite -q
python -m pytest tests/test_partition_integration.py -q -k 'not test_upsert_large_data'
git diff --check
```

结果：

- `88 passed in 45.55s`
- `234 passed, 13 skipped in 47.33s`
- `41 passed, 1 deselected in 34.92s`
- `git diff --check` 通过

## 边界

1. `cleanup_obsolete` 不证明 obsolete 数据已被新 generation 完全覆盖；它保证的是当前默认查询路径的可见 segment 深度校验通过，然后删除默认查询不再扫描的 obsolete 文件。
2. 不实现后台自动清理；生产环境仍应由运维任务显式调度。
3. 不实现跨进程 writer/cleanup 全局锁；同表维护操作仍建议串行化。
4. 不实现 tombstone、全局主键索引或 segment upsert。
5. `validate(deep=True)` 对大量 segment 会扫描 SQLite 文件和 stats，适合维护窗口或清理前执行，不应放在高频写入热路径。

## 下一步高价值目标

建议下一步做 **PLAN-045：流式查询与流式导出大结果集**。

原因：

1. 当前 `select()`、`query().to_list()` 和 CLI `query/export` 仍以一次性返回结果为主，文档也明确它们更适合小规模排查。
2. 金融海量数据的常见场景包括按时间窗口导出多年 K 线、批量回测读取、跨 symbol 扫描、迁移前抽样校验，这些都需要稳定的流式读取能力。
3. 已有 `iter_rows(batch_size=...)` 面向物理文件迁移，但还缺面向用户 where/order/limit 语义的流式查询入口。
4. 有了 compaction、cleanup 和迁移后，下一瓶颈会从“布局维护”转向“大结果集查询不应占满内存”。

建议范围：

- `table.iter_select(..., batch_size=...)`
- `table.query(...).iter_batches(batch_size=...)`
- CLI `export` 改为流式写 JSONL/CSV
- 保持 `select()` / `to_list()` 兼容小结果集
- 增加大结果集内存边界测试和 CLI 流式导出测试
