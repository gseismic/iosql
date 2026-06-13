# PLAN-016-row-bounded-segment-layout-OUTCOME

## 结论

本轮系统 review 面向金融高频时序数据，确认原实现的最大风险是：`PhysicalLayout` 只参与诊断，不参与写入决策；同一逻辑时间分区仍只能对应一个 SQLite 文件，无法约束单文件行数。

本次已落地第一阶段 row-bounded segment layout。新建时序表写入时，会在同一逻辑分区内按 `max_rows_per_file` 自动拆分多个物理 segment 文件；查询、`explain()`、`layout_report()` 和 `repartition_to()` 均能读取 segment 文件，旧单文件分区仍可读。

## 已完成

1. 新增 `SegmentInfo` 数据结构。
2. 新增 `_iosql_segments` manifest 表常量和读写逻辑。
3. `Table.write()` 通过 `insert_all()` 进入 segment writer：
   - 逻辑分区仍由原 `Partition` 决定。
   - 物理文件 key 使用 `{logical_part}__{segment_id:06d}`。
   - 每次写入后从实际 SQLite 文件回读 `COUNT(*)`、`MIN(time)`、`MAX(time)` 更新 manifest。
4. 查询路径改为扫描物理 refs：
   - legacy 单文件分区作为 `segment_id=-1` 继续读取。
   - segment manifest 文件按时间范围二次剪枝。
5. `query().explain()` 增加：
   - `candidate_segments`
   - `scanned_segments`
   - `skipped_segments`
   - `segment_skip_reasons`
6. `layout_report()` 改为物理文件粒度，避免把已拆分的逻辑分区总行数误判为单文件 oversized。
7. 新增公开 inspect 入口 `table.segments()`。
8. `create_index()`、`delete()`、`truncate()`、schema evolve 和 `repartition_to()` 均改为枚举物理 refs。
9. README 补充 segment layout、`segments()` 和 explain 说明。
10. 更新时序契约测试，覆盖单逻辑分区拆多个 segment、segment 级查询剪枝和公开 inspect 入口。

## 保留风险

1. 本阶段不实现持久化 WAL、崩溃恢复和并发写入协调。
2. `upsert/replace/ignore` 的跨 segment 全局主键定位仍未做专门优化。当前 manifest 行数以实际文件为准，但如果用户对已 sealed segment 中的主键再次写入，后续需要设计 segment 级主键索引或写前定位。
3. manifest 当前使用现有 `mysqlite` 简单 insert 封装，后续可改为参数化 SQL，减少元数据写入的字符串转义风险。

## 验证结果

已运行：

```bash
python -m pytest tests -q
python -m pytest tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py tests/test_partition_integration.py -q
python -m pytest tests/sqlite/test_timeseries_contract.py -q
python -m compileall -q iosql tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py
```

结果：

- 全量测试：`126 passed, 14 skipped`。
- 相关测试组：`60 passed`。
- 时序契约测试：`10 passed`。
- compileall 通过。

pytest cache warning 来自 `/Users/mac/primates/.pytest_cache` 写权限，不影响测试结果。
