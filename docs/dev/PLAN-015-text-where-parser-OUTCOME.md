# PLAN-015 实施结果

## 完成内容

1. 新增文本 where parser
   - 新增 `iosql/query/text_where.py`。
   - 支持比较操作：`=`, `==`, `!=`, `>`, `>=`, `<`, `<=`。
   - 支持 `between ... and ...`、`in (...)`。
   - 支持 `and`、`or` 和括号优先级。
   - 支持字符串、数字和布尔字面量。

2. 接入 `Where`
   - `Where("venue = 'NASDAQ'")` 会解析成内部 `Case`。
   - 文本 where 不会作为原始 SQL 拼接。
   - 不支持函数调用、子查询、注释、`like`、未加引号的字符串等 SQL 片段。

3. 接入 `Table.query()`
   - 修复字符串 where 与 `symbols/start/end/date` 自动条件组合的问题。
   - 组合逻辑会先把字符串 where 解析成 Or[And[Case]]，再和自动条件做 AND。

4. 示例和文档
   - orderbook 示例改为：
     `where="venue = 'NASDAQ'"`。
   - README 查询段落说明文本 where 的支持范围。

## Review 修复

- 首轮测试发现 `query(where="price >= 2", start=..., end=...)` 会在组合自动条件后失败，因为字符串被当成列表元素传入 `Where`。已在 `_combine_query_where()` 中先解析字符串 where，再追加自动生成条件。
- 编译检查第一次尝试覆盖整个 `tests/`，触发仓库已有备份文件 `tests/sqlite/_test_bkp.py` 的历史语法错误。最终编译检查改为覆盖本次相关路径：`iosql examples tests/query/test_text_where.py tests/sqlite/test_timeseries_contract.py`。

## 验证

已运行：

```bash
python -m pytest tests/query/test_text_where.py tests/sqlite/test_timeseries_contract.py -q
PYTHONPATH=. python examples/finance/02_orderbook_snapshots.py
python -m compileall -q iosql examples tests/query/test_text_where.py tests/sqlite/test_timeseries_contract.py
git diff --check
```

结果：

- 文本 where 单元测试和时序契约测试通过：`18 passed`。
- orderbook 示例使用文本 where 后运行通过。
- 编译检查和 whitespace 检查通过。
