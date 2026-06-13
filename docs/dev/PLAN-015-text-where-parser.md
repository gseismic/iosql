# PLAN-015 文本 where 解析

## 背景

当前 `Table.query(where=...)` 支持 dict、`Case`、`CaseExpression` 和列表形式。金融示例里筛选盘口交易所需要写成：

```python
where={"field": "venue", "op": "=", "value": "NASDAQ"}
```

用户希望也能写纯文本条件，并由程序内部解析，例如：

```python
where="venue = 'NASDAQ' and side in ('bid', 'ask')"
```

## 目标

1. `Where` 支持字符串表达式输入。
2. 文本表达式解析为内部 `Case` / `CaseExpression`，继续走现有 SQL 构造、字段校验、值转义和分区裁剪。
3. 支持常见查询语法：
   - 比较：`=`, `==`, `!=`, `>`, `>=`, `<`, `<=`
   - 集合：`field in (...)`
   - 范围：`field between a and b`
   - 布尔组合：`and`, `or`, 括号
   - 字符串、数字、布尔值
4. 不支持任意 SQL 片段，不允许函数调用、子查询、`like`、`is null`、注释等语法。

## 设计

- 新增 `iosql/query/text_where.py`，实现一个小型 tokenizer + 递归下降 parser。
- 解析优先级：括号 > comparison/between/in > `and` > `or`。
- 解析结果返回 `Case` 或 `CaseExpression`。
- `Where.__init__` 遇到 `str` 时调用 `parse_text_where()`。
- 空字符串视为无条件。
- 解析失败抛出 `BadArgument`，而不是把原字符串拼入 SQL。

## 验证

- 单元测试覆盖：
  - 简单等值条件。
  - `and` 条件。
  - `or` 条件。
  - 括号优先级。
  - `between` 和 `in`。
  - 不支持的 SQL 片段会失败。
- 时序表测试覆盖 `query(where="venue = 'NASDAQ'")`。
- 运行金融 orderbook 示例，使用文本 where。
- 运行 `python -m pytest tests/query/test_text_where.py tests/sqlite/test_timeseries_contract.py -q`。
- 运行 `python -m compileall -q iosql tests examples`。
- 运行 `git diff --check`。
