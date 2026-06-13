# PLAN-021-session-segment-pruning

## 背景

`PLAN-020` 已经让 `session=` 具备正确语义，但它目前只是普通 where 条件。查询计划只能通过时间范围做 segment 裁剪，不能利用 `trading_date` / `session` 字段跳过无关物理文件。

金融数据常按交易日查询，尤其夜盘、期货和跨时区行情。若一个逻辑时间分区内有多个 segment，且用户查询单个交易日，继续扫描全部 segment 会浪费 I/O，也让 `explain()` 难以解释 session 过滤的效果。

## 目标

1. segment manifest 增加 `session_min` / `session_max`。
2. 写入 segment 后刷新行数、时间范围和 session 范围。
3. `query(session=...)` 和文本/where 中对 session 列的 `=` / `in` 条件可参与 segment 级裁剪。
4. `query().explain()` 能报告 `pruned_by_segment_session_range`。
5. 旧 manifest 没有 session 字段时保持兼容，只是不做 session 裁剪。

## 非目标

1. 不记录完整 session set；本阶段只记录范围，适合 `YYYY-MM-DD` 这类可排序 session 值。
2. 不内置交易所日历。
3. 不做 session 级逻辑分区。

## 设计

`SegmentInfo` 新增：

```python
session_min: Optional[Any]
session_max: Optional[Any]
```

如果表声明了 `TableSpec.session_column`，刷新 manifest 时查询：

```sql
select MIN(session_column), MAX(session_column) from data
```

裁剪规则：

- `session = value`：如果 `value < session_min` 或 `value > session_max`，跳过 segment。
- `session in values`：如果所有值都落在 `[session_min, session_max]` 外，跳过 segment。

对缺失 session 范围的旧 segment 不做裁剪，保证兼容。

## 验证

1. session 查询只扫描 session 范围重叠的 segment。
2. `explain().segment_skip_reasons` 标注 `pruned_by_segment_session_range`。
3. `segments()` 返回 `session_min/session_max`。
4. 旧查询测试继续通过。
5. 运行：
   - `python -m pytest tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py -q`
   - `python -m pytest tests/sqlite -q`
   - `python -m compileall -q iosql examples tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py`
   - `git diff --check`
