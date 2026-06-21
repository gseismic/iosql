# PLAN-035-partition-string-spec-OUTCOME

## 实施结果

已完成基于字符串的 partition 表示，并将其接入 Python API、CLI、诊断输出、文档、示例和测试。

核心字符串表达：

```text
duration(ts,every=1day)
calendar(event_date,every=1day,tz=Asia/Shanghai)
range(id,width=100000)
```

## 主要变更

1. `iosql/spec.py`
   - 新增 `parse_partition_spec(...)` 和 `format_partition_spec(...)`。
   - 新增 `PartitionSpec.from_string(...)` 和 `PartitionSpec.to_string()`。
   - `TableContract` 支持 `partition` 直接接收字符串。
   - `TableContract.with_partition(partition="...")` 支持用完整字符串替换分区策略。
   - `range` 对外使用 `width`，内部继续映射到 `PartitionSpec.every`。
   - `duration` 字符串显式使用 `time_unit="ms"`，避免从旧 date/calendar 表继承错误 time unit。
   - `range` 不继承 `TimeColumn` 信息，避免输出和契约语义混淆。

2. `iosql/cli.py`
   - `repartition` 新增 `--partition`。
   - `--partition` 与 `--every/--kind/--timezone` 同时出现时拒绝。
   - `repartition` 安全语义调整为必须显式提供 `--target` 或 `--replace-source`。
   - 替换源库必须使用 `--replace-source --yes`；`--dry-run` 不要求 `--yes`。
   - `doctor` 的分区显示改为规范字符串。
   - `doctor` 的重分区建议改为可复制的 `--partition '...' --replace-source --yes`。
   - `repartition --dry-run` 和实际执行结果都输出 `old_partition_expr`、`new_partition_expr`、`final_uri`、`final_path`。
   - 修复目标库重分区时 `backup` 输出 `"None"` 字符串的问题，现在输出 JSON `null`。

3. `iosql/engines/sqlite/table.py`
   - 修复已有表再次写入时传入不同 runtime partition 会被静默忽略的问题。
   - 未显式 `force_partition` 时抛出 `TableSpecConflict`，错误消息保留 `PartitionCanNotChange`，与已有测试和“表契约不可静默改变”的设计一致。
   - CLI repartition 仍通过 `new_spec.force_partition = True` 在目标库/替换源库流程中显式允许新分区契约。

4. 文档和示例
   - 新增设计文档：`docs/design/partition-string-20260621-spec.md`。
   - 新增计划文档：`docs/dev/PLAN-035-partition-string-spec.md`。
   - 更新 `README.md` 和 `examples/README.md`。
   - 更新 `skills/iosql-usage/SKILL.md`，避免后续技能继续推荐旧 `--every` 入口。
   - 新增真实 CLI 示例：`examples/07_partition_string_repartition_cli.py`。

5. 测试
   - 新增 `tests/test_partition_string.py`。
   - 扩展 `tests/test_cli.py`，覆盖 `--partition`、安全参数冲突、doctor 建议和 `backup=null`。

## CLI 语义审查结论

原 `repartition` 的最大问题是默认替换源库路径不够明确：用户不传 `--target` 时，只要给 `--yes` 就会替换源库。现已改成显式意图模型：

```bash
# 安全生成新库
iosql repartition -s DB -t TABLE --partition 'duration(ts,every=1hour)' --target TARGET

# 明确替换源库
iosql repartition -s DB -t TABLE --partition 'duration(ts,every=1hour)' --replace-source --yes

# 替换源库前预演
iosql repartition -s DB -t TABLE --partition 'duration(ts,every=1hour)' --replace-source --dry-run
```

旧 `--every` 继续兼容，但帮助文本标记为兼容选项，并推荐使用 `--partition`。这样用户可以直接复制 `doctor` 输出，也能从 dry-run 结果看清旧分区、新分区和最终写入位置。

## Review 修复

1. 修复 `backup` 输出为字符串 `"None"` 的问题。
2. 调整参数校验顺序，使 `--partition` 与旧粒度参数冲突时优先提示冲突原因。
3. 修复从 date/calendar 表切换到 duration/range 时错误继承 time unit 的边界问题。
4. 修复已有表传入不同 runtime partition 时静默忽略的问题，改为抛出 `TableSpecConflict`。
5. 确认示例运行不会留下未跟踪的 runtime 数据。

## 验证

已运行：

```bash
python -m pytest tests/test_partition_string.py tests/test_cli.py
python -m pytest tests/test_partition.py tests/engines/sqlite/test_partitioned_table_contract.py tests/engines/sqlite/test_timeseries_contract.py
python -m pytest tests/test_partition_integration.py -x -vv
python -m pytest tests/core/query tests/engines/sqlite/test_partition_segment_combinations.py tests/engines/sqlite/test_query_correctness.py tests/engines/sqlite/test_classic_use.py tests/engines/sqlite/test_insert.py tests/test_partition.py
python -m pytest tests/utils tests/utils/test_utils.py
python -m pytest tests/engines/postgresql/test_postgresql.py tests/engines/sqlite/deprecated tests/engines/sqlite/test_advance.py tests/engines/sqlite/test_many_files.py
python -m pytest tests/test_partition_string.py tests/test_cli.py tests/test_partition_integration.py::TestPartitionChange::test_partition_change_rejected
python examples/07_partition_string_repartition_cli.py
python -m iosql.cli repartition --help
```

结果：

- `tests/test_partition_string.py tests/test_cli.py`：25 passed。
- `tests/test_partition.py tests/engines/sqlite/test_partitioned_table_contract.py tests/engines/sqlite/test_timeseries_contract.py`：71 passed。
- `tests/test_partition_integration.py -x -vv`：已明确通过到 `test_upsert_large_data` 前的所有测试，包括本轮修复的 `PartitionCanNotChange`；单独运行 `test_upsert_large_data` 60 秒无输出，已中断，未作为通过证据。
- `tests/core/query ... tests/test_partition.py`：86 passed, 1 skipped。
- `tests/utils tests/utils/test_utils.py`：43 passed, 1 skipped。
- `tests/engines/postgresql/test_postgresql.py tests/engines/sqlite/deprecated tests/engines/sqlite/test_advance.py tests/engines/sqlite/test_many_files.py`：12 skipped。
- 最终关键验证 `tests/test_partition_string.py tests/test_cli.py tests/test_partition_integration.py::TestPartitionChange::test_partition_change_rejected`：26 passed。
- 新增示例成功通过 CLI 生成 hourly 目标库。
- CLI help 显示 `--partition`、`--target`、`--replace-source` 的安全语义。
- 曾尝试运行完整 `python -m pytest`，进度通过此前失败位置，但长耗时大数据 upsert 用例未在可接受时间内完成，因此不声称完整套件通过。

## 与计划差异

1. 原计划只要求补充 `--partition`，实施中进一步调整了 `repartition` 安全语义，避免隐式替换源库。
2. 实际执行结果增加 `plan` 字段，让 dry-run 和实际执行输出结构更一致。
