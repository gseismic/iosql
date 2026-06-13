# PLAN-018-asof-latest-query

## 背景

系统 review 进入查询接口层。面向金融场景，真实使用频率很高的一类查询是“截至某个时间点，取每个标的最新一条记录”：

- 回测在每个 bar 时间点取组合内最新行情。
- 风控估值取某个截面上的最新价格。
- orderbook 或行情快照取每个 `symbol/venue` 的最新状态。
- 新闻和公告流取某个时间点前的最新事件。

当前用户只能手写 `query(end=..., order_by="ts desc")`，再在 Python 层按 symbol 去重。这段样板容易漏掉 symbol/time 投影、排序方向和过滤条件，且不利于后续统一优化。

## 目标

1. 新增 `table.latest(...)` 查询入口，返回每个 symbol 截至 `as_of` 的最新行。
2. 新增 `table.asof(...)` 作为金融常用别名。
3. 支持参数：
   - `symbols`
   - `as_of`
   - `columns`
   - `where`
4. 返回对象提供：
   - `to_list()`
   - `one()`
   - `explain()`
   - `spec()`
5. 与现有 `QuerySpec`、分区裁剪、segment 裁剪和文本 where 复用同一套查询路径。
6. 支持多 symbol 时每个 symbol 只返回一行，并按用户传入的 symbol 顺序输出。

## 非目标

1. 不实现 SQL 层窗口函数或跨 segment 并行优化。
2. 不支持 `group_by` 任意字段；本阶段只围绕 `TableSpec.symbol_column`。
3. 不引入交易日历、session calendar 或缺失值填充。
4. 不改变 `query()` 现有行为。

## 设计

新增 `_LatestQuery` 包装对象。内部生成普通 `QuerySpec`：

```python
QuerySpec(
    columns="*",
    symbols=symbols,
    end=as_of,
    where=where,
    order_by=f"{time_column} desc",
)
```

`to_list()` 使用现有查询路径读取候选行，再按 `symbol_column` 保留每个 symbol 的第一行。因为输入已按时间降序排序，第一行就是截至 `as_of` 最新记录。

如果 `symbols` 是单个值，`one()` 返回单行或 `None`；如果 `symbols` 是多个值，`one()` 在多结果时抛出 `BadArgument`，避免调用方误用。

## 验证

1. 单 symbol `latest(as_of=...)` 返回该 symbol 截至时间点的最新行。
2. 多 symbol `latest(symbols=[...])` 每个 symbol 返回一行，并保持输入顺序。
3. `latest(where="...")` 能和文本 where 组合。
4. `asof()` 是 `latest()` 的别名。
5. `explain()` 复用现有分区和 segment 裁剪信息。
6. 运行：
   - `python -m pytest tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py -q`
   - `python -m compileall -q iosql tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py`
   - `git diff --check`
