# PLAN-044 segment 校验与 obsolete 安全清理

## 背景

`PLAN-042` 已经实现 `compact()`：它会把当前可见 row segment 重写到新的 generation，并把旧 segment 标记为 `obsolete`。这解决了晚到数据造成的 segment 范围重叠和查询扫描膨胀，但旧 SQLite 文件仍然保守保留。

对于金融领域的海量 K 线、tick、逐笔成交、盘口快照、orderbook 增量、新闻事件、财报因子等数据，持续回补和整理会频繁产生 obsolete segment。如果没有可验证的清理路径，磁盘占用会持续上涨，运维上也无法证明“删除旧文件不会影响当前查询”。

## 目标

1. 新增表级 `validate(deep=False, include_obsolete=False)`，用于校验 row segment manifest 与物理文件。
2. `deep=True` 时检查：
   - manifest 中的当前可见 segment 文件存在；
   - SQLite `PRAGMA integrity_check`；
   - `data` 表存在；
   - manifest `row_count` 与物理 `COUNT(*)` 一致；
   - manifest `time_min/time_max`、`session_min/session_max` 与物理统计一致；
   - manifest `stats` 中记录的列级 min/max 与物理统计一致。
3. 新增 `cleanup_obsolete(partition=None, dry_run=True, validate=True)`：
   - 默认 dry-run，只输出候选文件和可释放字节数；
   - 真正删除需要显式 `dry_run=False`；
   - 删除前默认校验当前可见 segment，通过后才移除 obsolete 文件和 manifest 记录；
   - 不删除当前可见 segment；
   - 不处理非 segment 传统单文件表。
4. CLI 新增：
   - `iosql validate -s DB -t TABLE [--deep] [--include-obsolete]`
   - `iosql cleanup-obsolete -s DB -t TABLE [--partition PART] [--yes] [--no-validate]`
5. 更新 README、skill 和 API reference，明确安全边界。
6. 增加回归测试覆盖 Python API 与 CLI。

## 非目标

1. 不实现后台自动清理。
2. 不实现跨进程 writer/compaction 全局锁。
3. 不证明 obsolete segment 的数据已被新 generation 完全覆盖；本轮只保证当前可见 segment 自洽，且删除只针对查询默认不再扫描的 obsolete 文件。
4. 不清理 WAL/SHM 残留文件之外的任意未知文件。
5. 不实现 tombstone、全局主键索引或 segment upsert。

## API 设计

```python
validation = table.validate(deep=True)
assert validation.ok

preview = table.cleanup_obsolete()
print(preview.files, preview.bytes_reclaimable)

result = table.cleanup_obsolete(dry_run=False)
print(result.files_deleted, result.bytes_deleted)
```

返回对象：

```python
SegmentValidationResult(
    table="ticks",
    deep=True,
    include_obsolete=False,
    checked_segments=12,
    errors=[],
    warnings=[],
)

CleanupResult(
    table="ticks",
    dry_run=False,
    partitions=["__all__"],
    candidate_segments=4,
    files=4,
    files_deleted=4,
    bytes_reclaimable=104857600,
    bytes_deleted=104857600,
    manifest_rows_deleted=4,
    skipped=[],
)
```

## 实施步骤

1. 在 `iosql/spec.py` 新增 `ValidationIssue`、`SegmentValidationResult`、`CleanupResult`。
2. 在 SQLite `Table` 中实现：
   - `validate(...)`
   - `_validate_segment_ref(...)`
   - `_sqlite_integrity_check(...)`
   - `_delete_segment_manifest_rows(...)`
   - `cleanup_obsolete(...)`
3. CLI 增加 `validate` 和 `cleanup-obsolete`。
4. 更新 README 和 skill 文档。
5. 增加测试：
   - compact 后 validate 通过；
   - deep validate 能发现 manifest row_count 不一致；
   - cleanup 默认 dry-run 不删除；
   - cleanup 执行后 obsolete 文件和 manifest 记录被移除，当前查询结果不变；
   - CLI validate/cleanup-obsolete 行为。

## 验收标准

1. `table.validate(deep=True)` 能返回结构化结果并在正常 compact 后通过。
2. 人为篡改 manifest row_count 后，deep validate 返回错误。
3. `table.cleanup_obsolete()` 默认不删除文件。
4. `table.cleanup_obsolete(dry_run=False)` 只删除 obsolete 文件和对应 manifest，不影响查询结果。
5. CLI 输出 JSON，可用于运维脚本。
6. 相关测试通过，`git diff --check` 通过。
