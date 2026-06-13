# PLAN-019-latest-group-by

## 背景

`PLAN-018` 新增了 `latest()` / `asof()`，解决了“截至某时每个 symbol 的最新行”。继续面向金融真实场景 review 后发现，很多数据并不是只有 `symbol` 一个截面维度：

- K 线通常是 `symbol + timeframe + ts`。
- orderbook 快照通常是 `symbol + venue + ts`，有时还包含 `side/level`。
- 财报可能是 `symbol + statement`。
- 新闻事件可能按 `symbol + event_type` 查询。

当前 `latest()` 固定按 `TableSpec.symbol_column` 分组，会把同一个 symbol 下的多个 timeframe 或 venue 压成一行，导致截面查询不完整。

## 目标

1. `latest()` / `asof()` 增加 `by` 参数，支持字符串或字段列表。
2. 默认行为保持不变：`by=None` 时按 `TableSpec.symbol_column` 分组。
3. 多维分组时，按 `by` 字段组合保留截至 `as_of` 最新一行。
4. `symbols` 参数仍作为 symbol 过滤与返回排序依据。
5. 对多 symbol + 多维分组，返回顺序应优先尊重传入 symbol 顺序，再按数据中出现的分组顺序输出。
6. 文档和金融示例补充 `symbol + timeframe` 用法。

## 非目标

1. 不实现 SQL 窗口函数优化。
2. 不支持 arbitrary expression group by。
3. 不增加 `QuerySpec` 字段；`by` 只属于 `_LatestQuery` 的后处理语义。
4. 不把 `by` 作为过滤条件，过滤仍通过 `symbols` 和 `where` 表达。

## 设计

`latest(symbols=None, as_of=None, columns="*", where=None, by=None)`：

- `by=None`：等价于 `by=[symbol_column]`。
- `by="symbol"`：单字段分组。
- `by=["symbol", "timeframe"]`：组合字段分组。

内部仍复用普通 `QuerySpec`：

```python
QuerySpec(symbols=symbols, end=as_of, where=where, order_by="<time> desc")
```

`to_list()` 读取候选行后，用 `tuple(row[field] for field in by)` 作为分组 key。因为候选行已按时间倒序排列，第一次出现的 key 即最新行。

为了保持多 symbol 查询可预期：

1. 如果传入 `symbols`，输出先按 symbols 的输入顺序分桶。
2. 每个 symbol 内按候选行中首次遇到的分组顺序返回。
3. 如果没有传入 `symbols`，按候选行首次遇到的分组顺序返回。

## 验证

1. `latest(by=["symbol", "timeframe"])` 对同一 symbol 的多个 timeframe 都返回最新行。
2. `asof(by=["symbol", "venue"])` 对 orderbook 风格数据返回每个 venue 的最新行。
3. 默认 `latest()` 仍按 symbol 分组，旧测试保持通过。
4. 缺少 `by` 字段时抛 `BadArgument`，而不是静默返回错误结果。
5. 运行：
   - `python -m pytest tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py -q`
   - `python -m pytest tests/sqlite -q`
   - `python -m compileall -q iosql examples tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py`
   - `git diff --check`
