# PLAN-049 PostgreSQL calendar/date 分区策略硬化

## 背景

`PLAN-048` 已把 SQLite 到 PostgreSQL 的 duration 分区迁移流程通过示例和测试串起来。但 PostgreSQL native partition 下，`CalendarPartition.get_range()` 返回 UTC 毫秒边界，当前实现会直接把该整数边界用于 PostgreSQL range partition。

这对 duration/range 数值列是合理的，但对 calendar/date 场景不够安全：

- `columns={"date": "text"}` 且值为 `"2026-01-05"` 时，PostgreSQL range bound 应使用同样可比较的文本日期边界；
- `columns={"date": "date"}` 时，bound 应是 ISO date；
- `columns={"published_at": "timestamp"}` 时，bound 应是 timestamp；
- 用户不应理解或手动处理 native partition 子表边界。

## 目标

1. PostgreSQL native partition 为 `CalendarPartition` 生成与目标列类型匹配的 range bound。
2. 支持常见 calendar 列类型：
   - `text` / `str` / `string`；
   - `date`；
   - `timestamp` / `datetime`。
3. 对 duration/range 数值分区保持现有边界行为。
4. 增加无需真实 PostgreSQL 的 mock 测试，验证生成 partition SQL 的 bound 参数。
5. 可选真实 PostgreSQL 测试覆盖 text/date calendar 迁移，仍由 `POSTGRES_DSN` 控制。
6. 更新文档，删除“calendar 需要自行验证边界”的保守提示，改成明确支持范围和剩余边界。

## 非目标

1. 不支持任意自定义日期字符串格式到 PostgreSQL 原生 `date` 的所有转换。
2. 不改变 SQLite calendar 分区行为。
3. 不实现 `latest/asof/explain_select` 的 PostgreSQL 优化。
4. 不实现 checkpoint/resume。

## 设计

PostgreSQL `Table` 在 `use_contract()` 时已持有 `TableContract.columns`，因此可以通过 partition column 查到目标列类型。

新增内部方法：

```python
_partition_bounds(partition, part_key)
_calendar_partition_bounds(partition, part_key, column_type)
```

行为：

- duration/range：继续使用 `partition.get_range(part_key)`。
- calendar + text：
  - 若 partition key 形如 `YYYYMMDD`，源值常见为 `YYYY-MM-DD`，bound 输出 `YYYY-MM-DD` / 下一边界；
  - 月/年/hour/minute 根据 `CalendarPartition.fmt` 输出可排序字符串。
- calendar + date：输出 `YYYY-MM-DD`。
- calendar + timestamp/datetime：按 partition timezone 输出 ISO timestamp 字符串。

## 实施步骤

1. 在 PostgreSQL `Table` 中记录 `column_type` 并为 partition bound 增加类型感知转换。
2. 更新 `_insert_into_native_partition()` 使用 `_partition_bounds(...)`。
3. 增加 mock 测试：
   - text 日期列 `2026-01-05` bound；
   - date 列 bound；
   - duration 数值 bound 不变。
4. 可选真实 PostgreSQL 测试：
   - SQLite text calendar 源表迁移到 PostgreSQL；
   - `verify="row-count"`；
   - 目标侧查询。
5. 更新 README / skill / usage patterns。
6. 运行相关测试、示例、`git diff --check`。
7. Review 后生成 OUTCOME。

## 验收标准

1. calendar/text 源列迁移到 PostgreSQL 时 native partition bound 与文本日期可比较。
2. calendar/date 和 calendar/timestamp 目标列生成对应类型的 ISO 边界。
3. duration/range 迁移行为不回退。
4. 测试和文档证明用户无需手动理解 PostgreSQL partition 子表边界。
