# PLAN-020-session-column-query

## 背景

金融数据里的“日期”常有两种含义：

- 自然日：由时间戳和时区计算，例如 `2026-01-06 00:00:00` 到 `2026-01-06 23:59:59`。
- 交易日/session：由交易所规则定义，例如夜盘 `2026-01-05 21:00` 可能属于 `2026-01-06` 交易日。

当前 `query(date=...)` 使用 `TimeColumn.normalize_date_range()`，本质是自然日范围。对期货夜盘、跨时区市场、多 session 交易品种，用户需要能直接按数据里的 `trading_date` / `session` 列过滤，而不是把自然日误当交易日。

## 目标

1. `TableSpec` 增加 `session_column`，用于声明交易日/session 字段。
2. `Database.timeseries()` 和 `TableSpec.timeseries()` 支持 `session_column` 参数。
3. `query(session=...)` 支持按显式 session 列过滤。
4. 支持单个 session 和多个 session。
5. `session` 与 `symbols/start/end/date/where/latest/asof` 可组合。
6. 文档明确区分 `date` 是自然日，`session` 是用户显式声明的交易日/session 列。
7. 金融测试覆盖夜盘跨自然日但同交易日的查询。

## 非目标

1. 不内置交易所日历，不计算节假日、夜盘归属和半日交易。
2. 不改变 `date=` 现有语义。
3. 不强制表必须有 session 列。
4. 不做 session 级分区裁剪优化；本阶段先保证查询语义正确。

## 设计

`Database.timeseries()` 新增：

```python
session_column="trading_date"
```

`TableSpec` 持久化：

```python
{
  "session_column": "trading_date"
}
```

`Table.query()` 新增：

```python
table.query(session="2026-01-06")
table.query(session=["2026-01-06", "2026-01-07"])
table.latest(symbols="AU", session="2026-01-06", as_of=...)
```

编译为普通 where 条件：

- 单 session：`trading_date = value`
- 多 session：`trading_date in values`

如果调用 `query(session=...)` 但表没有声明 `session_column`，抛 `BadArgument`。

## 验证

1. 夜盘行和日盘行位于不同自然日/分区，但共享同一个 `trading_date`，`query(session=...)` 返回两行。
2. `query(date=...)` 仍按自然日范围，不被 session 逻辑改变。
3. `session` 可与 `symbols` 和文本 `where` 组合。
4. `TableSpec` 重新打开后保留 `session_column`。
5. 运行：
   - `python -m pytest tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py -q`
   - `python -m pytest tests/sqlite -q`
   - `python -m compileall -q iosql examples tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py`
   - `git diff --check`
