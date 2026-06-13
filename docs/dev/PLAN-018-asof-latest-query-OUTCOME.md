# PLAN-018-asof-latest-query-OUTCOME

## 结论

本轮 review 聚焦金融查询接口。当前 `query()` 已能表达时间窗口、symbol、where、排序和 limit，但真实金融工作流中常见的“截至某时每个 symbol 的最新行”仍需要用户手写排序和 Python 去重，容易重复造样板逻辑。

本次新增 `latest()` / `asof()` 截面查询入口，把该工作流提升为一等 API，并复用现有分区裁剪、segment 裁剪和 where 解析路径。

## 已完成

1. 新增 `_LatestQuery` 包装对象，提供：
   - `to_list()`
   - `one()`
   - `iter_rows()`
   - `explain()`
   - `spec()`
2. 新增 `Table.latest(symbols=None, as_of=None, columns="*", where=None)`。
3. 新增 `Table.asof(...)` 作为金融常用别名。
4. `latest()` 内部生成普通 `QuerySpec`：
   - `end=as_of`
   - `order_by=<time_column> desc`
   - `symbols` 和 `where` 原样复用
5. `to_list()` 按 `TableSpec.symbol_column` 保留每个 symbol 的第一行；多 symbol 查询按用户传入的 symbol 顺序返回。
6. `one()` 在返回多行时抛 `BadArgument`，避免误用。
7. 支持 `columns` 投影，内部仍读取完整行以保证 symbol/time 字段可用于分组。
8. README 补充 `latest()` 用法和返回语义。
9. 金融 K 线示例增加截面最新价查询。
10. 新增测试覆盖：
    - 单 symbol `latest(as_of=...)`
    - 多 symbol 输入顺序
    - `asof()` 别名
    - 文本 `where` 与 `as_of` 组合
    - `explain()` 仍使用 segment 时间剪枝

## 保留风险

1. 当前实现优先保证接口语义，内部仍在 Python 层按 symbol 去重；后续可用 SQL 窗口函数或 segment 级索引优化大规模截面查询。
2. 本阶段只支持按 `TableSpec.symbol_column` 分组，不做任意 `group_by`。
3. 对多维金融主键，例如 `symbol + venue + ts` 或 `symbol + timeframe + ts`，当前 `latest()` 仍按 symbol 取最新。如果需要 venue/timeframe 粒度，后续应增加 `by=` 或 `group_by=` 参数。

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

- 相关测试：`26 passed`。
- SQLite 测试组：`20 passed, 11 skipped`。
- 金融 K 线示例运行通过，输出截面最新价。
- compileall 通过。
- diff check 通过。

pytest cache warning 来自 `/Users/mac/primates/.pytest_cache` 写权限，不影响测试结果。
