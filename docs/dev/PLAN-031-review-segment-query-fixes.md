# PLAN-031-review-segment-query-fixes

## 背景

本轮 review 指出 `PLAN-029` 实现中的四个问题：

- Python 层 `order_by` 要求排序字段出现在投影字段中，不符合 SQLite 习惯。
- sealed segment 在删除后可能因 `row_count < max_rows` 被重新打开。
- `Segment.rows(...)` 下 `upsert/replace` 会直接修改 sealed segment，与当前未实现 pk index/tombstone 的设计冲突。
- `explain_select()` 的 `candidate_segments` 没有随 logical partition 裁剪收窄。

经代码核对，这四条均成立；第三条对 `write(mode="upsert")`、`write(mode="replace")` 以及 `conflict="upsert"/"replace"` 的契约路径成立，默认 append 路径不受影响。

## 目标

1. `select(fields, order_by=...)` 支持按未投影字段排序。
2. sealed segment 一旦封存，后续统计刷新和删除不会让它变回 active。
3. 在未实现全局 pk index/tombstone 前，`Segment.rows(...)` 拒绝 `upsert/replace`。
4. `explain_select()` 的 `candidate_segments` 只包含候选 logical partitions 下的 physical segments。
5. `write()` / `write_buffer()` 未显式指定 mode 时使用表契约 `conflict`，新表默认 append。
6. `repartition_to()` 默认用 append 复制到空目标库。
7. 补充回归测试，覆盖上述行为。

## 非目标

- 不实现全局 pk index。
- 不实现 tombstone。
- 不实现 compaction。
- 不改变 append-only segment 的默认语义。
- 不为 `Segment.rows(...)` 临时开放覆盖写。

## 验收

至少运行：

```bash
python -m pytest tests/sqlite/test_partitioned_table_contract.py tests/sqlite/test_partition_segment_combinations.py -q
python -m pytest tests/query tests/sqlite -q
```
