# time-first-repartition-suggestions-20260621-overview

## 背景

`iosql doctor` 当前只给出一条重分区命令。对于历史表，如果当前分区落在内部派生列 `partition_key` 上，输出会变成：

```bash
--partition 'range(partition_key,width=...)'
```

这对用户不友好：`range` 本身是合法的底层分区策略，但金融时序表的首要心智应是“按业务时间分区”。

## 目标

1. `doctor` 可以输出多条候选重分区建议，由用户选择。
2. 建议顺序优先按业务时间列，其次给自然周期，再给当前布局兼容方案。
3. 行数估算要透明：当当前 1day/1key 分区太小时，用目标行数除以当前平均行数，得到建议扩大倍数。
4. `range(...)` 保留为非时间数值字段或兼容旧布局的 fallback，不作为时序表首选建议。

## 分区表达语义

保留三个核心 kind：

| kind | 语义 | 适用场景 |
|------|------|----------|
| `calendar(column,every=...,tz=...)` | 按日期/日历时间字段分区 | `date`、`trade_date`、日期字符串 |
| `duration(column,every=...)` | 按毫秒时间戳定长分区 | `ts`、`timestamp` |
| `range(column,width=...)` | 按数值范围分区 | `id`、`offset`、内部 `partition_key` |

`calendar` 支持 `4day`、`1week`、`3month`、`5year` 这类可读周期。非 `1day/1month/1year` 的周期会以周期开始日或开始年月作为分区文件名。

## doctor 建议策略

### 当前分区太小

使用 `max_rows_per_file` 作为合并目标上限。例如：

```text
avg = 116,284
max_rows_per_file = 500,000
factor = floor(500,000 / 116,284) = 4
estimated = 116,284 * 4 = 465,136
```

输出建议：

1. 推荐：按时间列 `calendar(date,every=4day,tz=Asia/Shanghai)`。
2. 兼容：如果当前是 `range(partition_key,width=1)`，给出 `range(partition_key,width=4)`。
3. 当反推倍数足够大时，额外给自然周期建议，例如 `1week`、`1month`、`5year`。

### 当前分区太大

使用 `target_rows_per_file` 作为拆分目标。后续可继续细化为按当前 `every` 反推更小粒度。

## 时间列发现规则

1. 优先使用表契约 `time`。
2. 其次使用 `order_key`、主键和 columns 中的常见时间列名，如 `date`、`trade_date`、`event_date`、`ts`、`timestamp`。
3. 找不到时间列时，只输出当前分区类型的兼容建议。

## CLI 输出形态

```text
  undersized: 113 个分区行数不足 (min=200,000, avg=116,284)
  suggestions:
    1. 推荐: 按时间列 date 使用 4day (max/avg≈4, 目标<=500,000), 估算~465,136 行/分区
       → iosql repartition --source DB --table daily_bars --partition 'calendar(date,every=4day,tz=Asia/Shanghai)' --replace-source --yes
    2. 兼容: 保留当前数值分区键 partition_key，width=4 (扩大 4 倍), 估算~465,136 行/分区
       → iosql repartition --source DB --table daily_bars --partition 'range(partition_key,width=4)' --replace-source --yes
```

## 非目标

1. 不把 `segment` 混入 `partition` 字符串。
2. 不删除 `range` 能力；它仍适合非时间数值字段。
3. 不自动执行重分区；`doctor` 只输出建议命令。
