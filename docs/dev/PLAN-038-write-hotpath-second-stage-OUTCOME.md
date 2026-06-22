# PLAN-038-write-hotpath-second-stage-OUTCOME

## 实施摘要

完成 PLAN-037 提交后的第二阶段写入热路径优化：

1. `insert_all()` 直接复用 `do_insert_all()` 的实际分组结果生成 `partitions_touched`。
2. 每行 partition key 从计算两次降为一次。
3. `Segment.rows()` 新增可选 `stats_columns`。
4. 显式限制统计列时，系统自动保留 partition/time/order/session 必要字段。
5. 默认 `stats_columns=None` 仍统计全部列，保持兼容。
6. orderbook benchmark 和最佳实践改用 `stats_columns=["ts", "sequence"]`。

## 性能结果

相同开发机、相同 50 万行 profile：

| workload | PLAN-037 后 | PLAN-038 后 | 变化 |
|----------|------------:|------------:|-----:|
| 日线，batch=50000 | 7.49s | 4.62s | -38.3% |
| orderbook，batch=100000 | 6.17s | 3.04s | -50.7% |

细分：

- 日线 `CalendarPartition.get_key()` 调用从 100 万次降为 50 万次。
- orderbook `DurationPartition.get_key()` 调用从 100 万次降为 50 万次。
- orderbook `_batch_column_stats()` 从约 1.40 秒降到约 0.18 秒。
- orderbook stats 从 7 列限制为 `ts`、`sequence` 两列。

这些是开发机相对结果，不替代 `/media/lsl/G1` ext4 HDD 的最终绝对吞吐。

## 正确性

新增测试覆盖：

- 自定义 counting partition 验证每行只调用一次 `get_key()`。
- `WriteResult.partitions_touched` 保持排序和原语义。
- `Segment.stats_columns` 去重、序列化和恢复。
- 必要的 partition/order key 自动补入 stats。
- 已统计字段继续正确裁剪 segment。
- 未统计字段保守扫描全部候选 segment。
- 未配置 `stats_columns` 的旧行为继续统计全部列。

验证结果：

```text
最终非 large：
251 passed, 14 skipped, 3 deselected in 7.32s

写入/segment/timeseries/benchmark 专项：
88 passed in 2.89s

3 个历史 TestLargeData：
3 passed in 256.10s

最终合计：
254 passed, 14 skipped
```

同时通过：

- `python -m compileall iosql benchmarks -q`
- `git diff --check`

## 文档

已更新：

- `README.md`
- `skills/iosql-usage/SKILL.md`
- `skills/iosql-usage/references/write-performance.md`
- `skills/iosql-usage/references/api-reference.md`
- `benchmarks/benchmark_write.py`
