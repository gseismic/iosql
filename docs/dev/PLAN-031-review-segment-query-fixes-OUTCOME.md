# PLAN-031-review-segment-query-fixes-OUTCOME

## 结论

本轮 review 的四条发现均成立，已修复并补充回归测试。

其中第三条需要补充说明：默认 append 路径本身不会主动修改 sealed segment；风险来自显式 `write(mode="upsert")`、`write(mode="replace")`、`insert_all(replace=True)` 以及表契约 `conflict="upsert"/"replace"`。当前阶段没有全局 pk index/tombstone，因此这些路径在 `Segment.rows(...)` 表上统一拒绝。

## 变更

1. 查询排序
   - `select(["symbol", "value"], order_by="seq")` 会内部补齐 `seq` 用于 Python 层全局排序。
   - 排序和 `limit` 之后再剥离内部补齐字段，返回结果不泄漏未投影列。

2. sealed segment
   - `_refresh_segment_info()` 会读取已有 segment metadata。
   - `sealed=True` 一旦成立会持久保持，删除只更新行数和统计，不重新打开旧 segment。

3. segment 覆盖写
   - `Segment.rows(...)` 表在未实现全局 pk index/tombstone 前拒绝 `upsert/replace`。
   - 覆盖路径包括 `write(mode="upsert")`、`write(mode="replace")`、`insert_all(replace=True)` 和表契约 `conflict="upsert"/"replace"`。
   - `write()` 和 `write_buffer()` 未显式传 mode 时使用表契约 `conflict`；新表默认 `append`。

4. 查询计划
   - `explain_select()` 的 `candidate_segments`、`scanned_segments`、`skipped_segments` 只基于候选 logical partitions 计算。
   - 避免 logical partition 已被裁剪但仍出现在候选 physical segment 里的误导性计划。

5. 重分区和文档
   - `repartition_to()` 默认 mode 改为 `append`，符合向空目标库复制数据的语义。
   - 更新 `skills/iosql-usage` 的 API、使用模式和排查文档。
   - 更新 `examples/README.md`，说明当前 row segment 示例是 append-first，不演示跨 segment 覆盖写。

## 测试

已运行：

```bash
python -m compileall iosql -q
python -m pytest tests/sqlite/test_partitioned_table_contract.py tests/sqlite/test_partition_segment_combinations.py tests/sqlite/test_timeseries_contract.py -q
python -m pytest tests/query tests/sqlite -q
python examples/03_buffered_ingest_and_query_plan.py
python examples/06_row_segment_late_data.py
python examples/04_layout_report_and_repartition.py
```

结果：

- `compileall` 通过。
- 目标 sqlite 测试：78 passed。
- `tests/query tests/sqlite`：90 passed, 12 skipped。
- 三个代表性示例均运行成功。
- pytest 有一条缓存写入 warning：沙箱无法写 `/Users/mac/primates/.pytest_cache/...`，不影响测试结果。

## 后续

后续如需开放 `Segment.rows(...)` 的 `upsert/replace`，应先实现全局 pk index、tombstone 和 compaction，再恢复 keyed/upsert segment 表契约。
