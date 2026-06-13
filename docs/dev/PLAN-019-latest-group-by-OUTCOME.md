# PLAN-019-latest-group-by-OUTCOME

## 结论

本轮 review 继续聚焦金融截面查询。`PLAN-018` 的 `latest()` 默认按 symbol 分组，适合简单行情表；但 K 线、orderbook、财报等数据通常有多维主键，例如 `symbol + timeframe`、`symbol + venue`、`symbol + statement`。只按 symbol 分组会把同一 symbol 下的多个维度压成一行，导致截面查询不完整。

本次已增加 `by` / `group_by` 分组参数，让 `latest()` / `asof()` 支持多维金融截面。

## 已完成

1. `Table.latest()` 增加参数：
   - `by=None`
   - `group_by=None`
2. `Table.asof()` 同步支持 `by` / `group_by`。
3. 默认行为保持不变：不传 `by` 时仍按 `TableSpec.symbol_column` 分组。
4. 支持字符串和字段列表：
   - `by="symbol"`
   - `by=["symbol", "timeframe"]`
   - `group_by=["symbol", "venue"]`
5. `by` 和 `group_by` 同时传入且冲突时抛 `BadArgument`。
6. 分组字段缺失时抛 `BadArgument`，避免静默合并或返回错误截面。
7. 多 symbol 查询仍优先按传入 symbol 顺序返回；每个 symbol 内按候选行首次遇到的分组顺序输出。
8. README 补充多维截面写法。
9. 金融 K 线示例改为显式使用 `by=["symbol", "timeframe"]`。
10. 新增测试覆盖：
    - `by=["symbol", "timeframe"]`
    - `by=["symbol", "venue"]`
    - `group_by` 别名
    - `by/group_by` 冲突
    - 缺失分组字段错误

## 保留风险

1. 当前实现仍在 Python 层分组，优先保证接口语义；超大截面查询后续可用 SQL 窗口函数或索引缓存优化。
2. 分组字段只参与后处理，不会自动成为查询过滤条件；过滤仍通过 `symbols` 和 `where` 表达。
3. 尚未引入交易日历/session 语义。下一轮更高价值方向是把 `date/session` 从自然日提升为金融交易日/session 级接口。

## 验证结果

已运行：

```bash
python -m pytest tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py -q
python -m pytest tests/sqlite -q
PYTHONPATH=. python examples/finance/01_kline_bars.py
python -m compileall -q iosql examples tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py
git diff --check
```

结果：

- 相关测试：`31 passed`。
- SQLite 测试组：`25 passed, 11 skipped`。
- 金融 K 线示例运行通过，输出 `symbol + timeframe` 截面最新价。
- compileall 通过。
- diff check 通过。

pytest cache warning 来自 `/Users/mac/primates/.pytest_cache` 写权限，不影响测试结果。
