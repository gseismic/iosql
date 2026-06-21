# PLAN-035-partition-string-spec

## 目标

为 iosql 定义并实现基于字符串的 partition 表示，使 CLI、Python API 和诊断建议使用同一套可解析、可格式化、可复制执行的表达。同时重新审查 CLI 语义，优先优化 `repartition` 及其相关诊断输出，让重分区操作的输入、计划、风险和结果更明确。

## 设计依据

设计文档：`docs/design/partition-string-20260621-spec.md`

核心表达：

```text
duration(ts,every=1day)
calendar(date,every=1day,tz=Asia/Shanghai)
range(partition_key,width=1107)
```

## 实施步骤

1. 在 `PartitionSpec` 层新增字符串解析和格式化能力。
   - 支持 `PartitionSpec.from_string(...)`。
   - 支持 `PartitionSpec.to_string()`。
   - 提供公共函数 `parse_partition_spec(...)` 和 `format_partition_spec(...)`。
   - `range` 对外使用 `width`，内部映射到现有 `PartitionSpec.every`。

2. 接入表契约。
   - `TableContract.__post_init__` 支持 `partition` 为字符串。
   - `Database.table(..., partition="...")` 可直接声明分区。
   - 保持已有 runtime `Partition` 对象和 dict 契约兼容。

3. 接入 CLI。
   - `iosql repartition` 新增 `--partition`。
   - `--partition` 与 `--every/--kind/--timezone` 同时出现时拒绝，避免歧义。
   - `doctor` 显示 `format_partition_spec(...)`。
   - `doctor` 推荐命令改用 `--partition '...'`。
   - `repartition --dry-run` 输出 `old_partition_expr` 和 `new_partition_expr`。
   - `repartition` 的帮助文本明确安全意图：`--target` 写新库，`--replace-source --yes` 才替换源库。
   - 输出计划和结果中同时保留机器可读字段和用户可读 partition 字符串。

4. 审查并优化 CLI 语义。
   - 检查 `doctor`、`repartition`、`layout`、`spec`、`index` 等命令的参数命名、帮助文本和输出是否清晰。
   - 不做大规模破坏性重命名，优先通过新增选项、输出字段、帮助文本和建议命令降低误解。
   - 对安全敏感命令保留显式确认和 dry-run 路径。

5. 更新文档和示例。
   - README 增加字符串 partition 说明。
   - `skills/iosql-usage/SKILL.md` 更新 CLI 和 API 快速说明。
   - 新增真实场景示例，演示用字符串声明分区并重分区。

6. 补充测试。
   - 测试字符串解析、格式化和错误处理。
   - 测试 Python API 接收字符串 partition。
   - 测试 CLI `--partition` dry-run 和实际 repartition。
   - 测试 `doctor` 输出可复制的 `--partition` 建议。
   - 测试 `--partition` 与旧粒度参数的冲突提示。

7. 验证和交付。
   - 运行相关测试。
   - 运行新增示例。
   - 代码 review 后修复问题。
   - 输出 `docs/dev/PLAN-035-partition-string-spec-OUTCOME.md`。
   - 按规范提交并推送。

## 风险与约束

1. 本轮不改变存储布局，仅新增输入/输出表示。
2. 旧 `--every` 继续保留兼容，但不再作为推荐路径。
3. 字符串解析保持小语法，不支持 URI 风格和复杂转义。
4. `calendar fmt` 只作为兼容底层元数据的高级选项，常规用户优先写 `every`。
