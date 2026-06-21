# PLAN-036-time-first-doctor-repartition

## 目标

优化 `iosql doctor` 的重分区建议，使时序表优先推荐业务时间分区，并输出多条可选建议。避免把历史内部列 `partition_key` 上的 `range(...)` 作为唯一推荐。

## 计划

1. 设计分区建议语义
   - 新增设计文档 `docs/design/time-first-repartition-suggestions-20260621-overview.md`。
   - 明确 `calendar` / `duration` / `range` 的职责。
   - 明确 `range` 是非时间数值字段或兼容旧布局 fallback。

2. 扩展 calendar 字符串能力
   - 支持 `calendar(date,every=4day,tz=Asia/Shanghai)`。
   - 支持 `1week`、`2week`、`3month`、`5year` 等自然周期。
   - 保持旧 `fmt` 元数据兼容。

3. 重写 `doctor` 建议策略
   - 增加时间列发现逻辑：优先 `TableSpec.time`，其次常见时间列名。
   - 对 undersized 分区使用 `max_rows_per_file / avg_rows` 反推合并倍数。
   - 输出多条建议：推荐时间粒度、自然时间周期、当前分区兼容方案。
   - 保持 oversized 的基本建议能力。

4. 补测试
   - 测试 `calendar(...,every=4day)` 解析、格式化、分区路由。
   - 测试 `doctor` 对 `time=date + partition_key range` 表优先推荐 `calendar(date,...)`。
   - 测试 `doctor` 仍为非时间 range 表输出 range fallback。

5. 更新文档和例子
   - 更新 README / examples 说明。
   - 必要时更新 iosql skill 文档。

6. Review、测试、结果与提交
   - 运行相关 pytest。
   - 运行示例或 CLI smoke。
   - 生成 `PLAN-036-time-first-doctor-repartition-OUTCOME.md`。
   - 使用中文 commit message，提交并推送。
