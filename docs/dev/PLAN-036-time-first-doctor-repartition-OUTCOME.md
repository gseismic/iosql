# PLAN-036-time-first-doctor-repartition-OUTCOME

## 实施结果

本次完成了 `iosql doctor` 重分区建议的 time-first 优化。核心变化：

1. `doctor` 不再只输出单条 `range(partition_key,...)` 建议。
2. 对带 `time` 契约或可推断时间列的表，优先输出 `calendar(...)` 或 `duration(...)` 建议。
3. undersized 分区使用 `max_rows_per_file / avg_rows` 反推合并倍数，避免继续使用不透明的 `--every`。
4. 对历史 `range(partition_key,width=1)` 表，保留 `range(...)` 作为兼容 fallback。
5. `calendar` 支持 `4day`、`1week`、`1month`、`5year` 这类周期表达。
6. `duration` 的整数毫秒输出会格式化为 `5day`、`2hour` 等可读单位。

## 关键行为

历史日频表如果契约中存在：

```json
{
  "time": {"name": "date", "unit": "date", "timezone": "Asia/Shanghai"},
  "partition": {"column": "partition_key", "kind": "range", "every": 1}
}
```

`doctor` 会优先输出：

```bash
iosql repartition --source DB --table daily_bars --partition 'calendar(date,every=4day,tz=Asia/Shanghai)' --replace-source --yes
```

并把旧布局兼容方案放在后面：

```bash
iosql repartition --source DB --table daily_bars --partition 'range(partition_key,width=4)' --replace-source --yes
```

对于没有时间语义的纯数值表，仍推荐：

```bash
iosql repartition --source DB --table items --partition 'range(id,width=500000)' --replace-source --yes
```

## 变更文件

- `iosql/cli.py`
  - 新增 time-first 分区建议策略。
  - 新增多条 suggestions 输出。
  - undersized 目标改为用 `max_rows_per_file` 反推合并倍数。

- `iosql/partition/calendar.py`
  - 支持多日、多周、多月、多年 calendar bucket。
  - 保持旧 `fmt/tz/ts_col` 元数据兼容；只有非默认周期才记录 `every`。

- `iosql/spec.py`
  - `calendar(...)` 字符串支持扩展周期。
  - `duration(...)` 毫秒值格式化为可读周期。

- `examples/08_doctor_time_first_suggestions.py`
  - 新增真实 CLI 示例，展示历史 `partition_key` 表的 time-first doctor 输出。

- `tests/test_cli.py`
  - 覆盖 time-first 建议、range fallback、duration baseline 边界。

- `tests/test_partition_string.py`
  - 覆盖 `calendar(...,every=4day)` 和 duration 可读格式化。

- `README.md`、`examples/README.md`、`skills/iosql-usage/SKILL.md`
  - 更新 partition 心智模型和 doctor 建议说明。

## 验证

已通过：

```bash
python -m pytest tests/test_partition_string.py tests/test_cli.py tests/test_partition.py tests/engines/sqlite/test_partitioned_table_contract.py tests/engines/sqlite/test_partition_segment_combinations.py tests/engines/sqlite/test_timeseries_contract.py tests/engines/sqlite/test_query_correctness.py tests/test_partition_integration.py::TestPartitionChange::test_partition_change_rejected
```

结果：`145 passed`

已运行示例：

```bash
python examples/07_partition_string_repartition_cli.py
python examples/08_doctor_time_first_suggestions.py
```

已通过：

```bash
git diff --check
```

## Review 结论

Review 中发现并修复了一个边界问题：如果表有 `ts` 时间列但当前分区是 `range(partition_key)`，duration 建议不能用 range width 当毫秒间隔缩放。现已改为只有当前分区本身是 duration 时才按旧 duration 缩放，否则从 `1day` 作为基准。

另一个修复是 duration 文案格式化：`432000000ms` 会输出为 `5day`，避免 CLI 建议再次变得不透明。
