# PLAN-042 Row Segment Compaction

## 背景

`Segment.rows(...)` 已经支持按行数滚动物理 SQLite 文件，并通过 segment stats 做二级查询裁剪。金融 K 线、tick、orderbook 等场景会遇到两类长期问题：

1. 晚到数据或历史回补会写入新的 active/delta segment，导致同一 logical partition 下的 stats 范围重叠。
2. 长期追加、删除或小批量写入会产生过多小 segment，查询需要打开更多 SQLite 文件。

当前设计明确要求 compaction 作为 row segment 生命周期的一部分，但实现尚未落地。`Segment.rows(...)` 的 `upsert/replace` 仍被拒绝，后续要支持全局 pk index 和 tombstone，也需要先有安全的 generation 切换与 obsolete segment 清理路径。

## 目标

1. 新增 `Table.compact(...)`，对 SQLite `Segment.rows(...)` 表执行显式整理。
2. compaction 只重写当前可见的 active/sealed segment，不扫描已 obsolete segment。
3. 新 segment 按 `order_key`、时序 time 字段或主键排序，重新按 `target_rows_per_file` 切分。
4. 保留 append-only 语义：如果历史数据存在跨 segment 重复主键，重写时不能把重复主键写入同一 SQLite 文件。
5. 写入新 generation 的 sealed segment，成功后把旧 segment 标记为 `obsolete`。
6. 查询、`segments()`、`layout_report()`、`repartition_to()` 默认只使用非 obsolete segment。
7. 增加针对金融 K 线晚到数据的回归测试，验证 compact 前后查询结果一致，并改善 segment 裁剪/布局。
8. 更新 README、skill 和 API reference，说明 compaction 的边界。

## 非目标

1. 不在本轮开放 `Segment.rows(...)` 的 `upsert/replace`。
2. 不实现全局 pk index、tombstone 或按版本去重。
3. 不后台自动 compact；本轮只提供显式 API。
4. 不物理删除 obsolete 文件；先保守保留，避免活跃查询或异常恢复风险。
5. 不实现 PostgreSQL compaction。

## API

```python
result = table.compact(
    partition=None,          # None 表示所有 logical partition
    target_rows=None,        # 默认使用 spec.physical_layout.target_rows_per_file
)
```

返回 `CompactResult`：

```python
CompactResult(
    table="ticks",
    partitions=["0"],
    rows_read=1000,
    rows_written=1000,
    old_segments=8,
    new_segments=2,
    obsolete_segments=8,
)
```

`segments(include_obsolete=True)` 可用于运维检查 obsolete segment；默认仍只返回当前可见 segment。

## 实施步骤

1. 扩展 `SegmentInfo` 和 manifest：
   - 新增 `state` 字段，兼容旧库缺省值；
   - 默认非 obsolete；
   - `to_dict()` 输出 `state`。
2. 新增 `CompactResult` 并从 `iosql.__init__` 导出。
3. 修改 segment 读取路径：
   - `_segment_infos(..., include_obsolete=False)` 默认过滤 obsolete；
   - `_physical_part_refs()`、查询、诊断和写入使用默认过滤；
   - 新 segment id 基于包含 obsolete 的最大 id 继续递增，避免 manifest 主键冲突。
4. 实现 `Table.compact(...)`：
   - 校验表存在、表契约存在且启用 `Segment.rows(...)`；
   - 选择 logical partitions；
   - 读取当前可见 segment 行；
   - 按金融查询友好的顺序排序；
   - 按 `target_rows` 分块，且避免同一新 chunk 内出现重复主键；
   - 写入新的 sealed generation segment；
   - 成功后把旧 segment 标记 obsolete；
   - 异常时尽量把新 segment 标记 obsolete，不改变旧 segment 可见性。
5. 增加测试：
   - 晚到 K 线 compact 后结果不变，segment 数下降；
   - compact 后 query/explain 不再扫描 obsolete segment；
   - 跨 segment 重复主键 compact 后仍可查询到重复记录；
   - `segments(include_obsolete=True)` 能看到旧 segment 状态；
   - 非 segment 表调用 compact 抛错。
6. 更新文档和 skill。
7. 运行相关 SQLite 契约测试、CLI/README smoke 和 compileall，生成 OUTCOME。

## 验收标准

- `table.compact()` 对金融 K 线 late data 场景保持查询结果一致。
- compact 成功后默认查询不扫描 obsolete segment。
- compact 失败不会让旧 segment 从默认查询路径消失。
- `segments()` 默认只返回当前可见 segment；`segments(include_obsolete=True)` 返回历史 segment 状态。
- 相关测试通过，并有 OUTCOME 记录验证结果与下一步目标。
