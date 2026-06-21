# partition-string-20260621-spec

## 背景

当前 CLI 使用 `--every` 修改分区粒度，但 `every` 在不同分区类型下含义不同：

- `duration`：固定时间长度，例如 `1day`。
- `calendar`：日历粒度，例如 `1month`。
- `range`：数值宽度，例如 `1107` 个 `partition_key` 单位。

这导致 `doctor` 输出类似 `--every 1107` 时，人无法判断它是时间、行数还是数值宽度。需要定义一个稳定的字符串契约，让 CLI 输出、CLI 输入、Python API 和文档使用同一套表示。

## 目标

1. 分区配置可用一个字符串完整表达。
2. 字符串可读、可复制、可解析、可格式化回写。
3. `doctor` 输出的建议可以直接传给 `repartition --partition`。
4. 不把 `segment` 混入 `partition` 字符串，保持逻辑分区和物理分段职责分离。
5. 兼容现有 `Partition.duration(...)`、`Partition.calendar(...)`、`Partition.range(...)` 对象 API。

## 字符串语法

采用函数式表达：

```text
partition = kind "(" column ["," option]* ")"
option    = key "=" value
```

支持的 `kind`：

```text
duration(ts,every=1day)
calendar(date,every=1day,tz=Asia/Shanghai)
range(partition_key,width=1107)
```

### `duration`

```text
duration(column,every=1day)
```

- `column`：毫秒时间戳列。
- `every`：固定时长，支持 `1min`、`5min`、`1hour`、`1day`、`1week` 等当前 `DurationPartition` 已支持的别名，也支持 `86400000ms` 这类显式毫秒表达。
- 默认值：`every=1day`。

### `calendar`

```text
calendar(column,every=1day,tz=Asia/Shanghai)
```

- `column`：日期或时间字符串列。
- `every`：日历粒度，支持 `1hour`、`1day`、`1month`、`1year`。
- `tz` / `timezone`：日历边界时区。
- `fmt`：底层日期格式，通常由 `every` 推导；仅在已有元数据无法用 `every` 表达时显示。
- 默认值：`every=1day`，`tz` 不写时按现有 `PartitionSpec` 的默认行为处理。

### `range`

```text
range(column,width=100000)
```

- `column`：数值列。
- `width`：每个逻辑分区覆盖的数值宽度。
- 不使用 `every`，因为 `range` 的核心语义是数值宽度，不是时间周期。

## 规范化输出

`format(parse(expr))` 应输出稳定字符串：

```text
duration(ts,every=1day)
calendar(date,every=1day,tz=Asia/Shanghai)
range(partition_key,width=1107)
```

现有元数据如果以毫秒保存 duration，例如 `86400000`，格式化时应优先输出常用别名 `1day`。现有 range 元数据内部仍保存到 `PartitionSpec.every`，但对外格式化为 `width`。

## CLI 设计

新增：

```bash
iosql repartition -s DB -t TABLE --partition 'duration(ts,every=1hour)' --target TARGET
iosql repartition -s DB -t TABLE --partition 'calendar(date,every=1month,tz=Asia/Shanghai)' --target TARGET
iosql repartition -s DB -t TABLE --partition 'range(partition_key,width=1107)' --replace-source --yes
```

兼容保留：

```bash
iosql repartition -s DB -t TABLE --every 1hour --target TARGET
```

兼容路径含义：沿用旧分区的 `kind/column/timezone/fmt`，只替换内部粒度字段。新文档和 `doctor` 建议不再推荐 `--every`。

后续 `docs/design/time-first-repartition-suggestions-20260621-overview.md` 对 `doctor` 建议策略做了进一步优化：时序表优先推荐 `calendar(...)` / `duration(...)`，`range(partition_key,...)` 只作为兼容 fallback。

旧 `doctor` 输出示例：

```text
partition: range(partition_key,width=1)
undersized: 422 个分区行数不足 (min=200,000, avg=316)
  → iosql repartition --source DB --table adjustment_factors --partition 'range(partition_key,width=1107)' --replace-source --yes  (~349,812 行/分区)
```

## Python API 设计

允许 `Database.table(..., partition=...)` 接收字符串：

```python
db.table(
    "ticks",
    pk=["symbol", "ts"],
    partition="duration(ts,every=1day)",
)

db.table(
    "items",
    pk=["id"],
    partition="range(id,width=100000)",
)
```

内部统一解析为 `PartitionSpec`，再转换为现有 runtime `Partition` 对象。

## 非目标

1. 不设计 URI 语法，避免 `tz=Asia/Shanghai` 等值带来转义复杂度。
2. 不改变目录布局和分区 key 计算方式。
3. 不把 row segment 表达为 partition。
4. 不在本轮实现新的分区类型，例如 `hash`、`value`。
