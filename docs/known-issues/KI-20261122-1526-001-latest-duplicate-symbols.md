# KI-20261122-1526-001 · latest 重复 symbol 的跨后端结果差异

- 2026-11-22 15:26（Asia/Shanghai）创建
- 模型为 Codex。环境说明基于 GPT-6，未提供可核实的实际模型 ID。
- 初始状态为未解决
- 来源为 SQLite 升级 Review 修复期间扩展执行 PostgreSQL 集成回归
- 确认版本为基准 `ffb9e603dc51941817f4e79fe5a26bf742d0ba39`。独立归档副本、PostgreSQL 14.19。

同一批请求 `latest(symbols=['B', 'A', 'B'])` 在目录后端按首次命中排序并对相同分组返回一行，PostgreSQL 按请求重复返回同一组。在基准副本中运行 `tests/engines/postgresql/test_query_performance.py::test_latest_semantics_match_sqlite[kwargs2]` 可复现失败。该文件的 `test_numeric_symbols_bound_in_values` 又明确要求 PostgreSQL 对 `['2', '1', '2']` 返回 `[2, 1, 2]`。具体引入版本尚未定位。

两条断言涉及公开行为兼容性，本轮保留 PostgreSQL 的重复请求语义。需要跨后端结果一致时，调用方可按输入顺序先去掉重复请求。若未来统一行为，应先决定重复请求的契约并提供升级说明。

## 2026-11-22 15:32 引入版本核对

模型同上。独立归档的父提交 `f4cd8431e7e14bd6ce8ea5b2a25a8156d2ae445d` 在同一 PostgreSQL 14.19 实例上通过上述两项断言。`ffb9e603dc51941817f4e79fe5a26bf742d0ba39` 把 SQLite 改为合并等价请求，跨后端对照自该提交起失败。状态仍为未解决。
