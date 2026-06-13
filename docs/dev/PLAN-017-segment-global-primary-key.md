# PLAN-017-segment-global-primary-key

## 背景

`PLAN-016` 完成了按行数拆分物理 segment，但 review 发现一个金融场景下的正确性问题：同一逻辑时间分区被拆成多个 segment 后，SQLite 的主键约束只在单个物理文件内生效。

对于 K 线修订、tick 重放、orderbook 快照补写这类真实场景，用户通常依赖 `primary_key` 表达逻辑表唯一性。当前默认 `write(mode="upsert")` 如果命中已 sealed 的旧 segment，会把修订行写入新 segment，导致查询返回重复逻辑主键。

## 复现

```python
ticks = db.timeseries(
    "ticks",
    time="ts",
    primary_key=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "price": "float"},
    max_rows_per_file=1,
)
ticks.write([
    {"symbol": "AAPL", "ts": 1000, "price": 1.0},
    {"symbol": "MSFT", "ts": 2000, "price": 2.0},
])
ticks.write({"symbol": "AAPL", "ts": 1000, "price": 9.0})
```

预期只有一行 `AAPL/1000`，实际会跨 segment 出现两行。

## 目标

1. segment writer 写入前按主键在当前逻辑分区的物理 refs 中定位已有行。
2. `upsert` / `replace` 命中已有主键时写回原 segment，避免重复逻辑主键。
3. `ignore` 命中已有主键时跳过，不创建新 segment。
4. `append` 命中已有主键时写回原 segment 并沿用 SQLite 主键错误语义，避免绕过逻辑主键。
5. 新增测试覆盖跨 segment 修订、忽略和同批重复主键。

## 非目标

1. 不实现全局主键索引表；本阶段先通过当前逻辑分区内 segment 扫描保证正确性。
2. 不优化大批量回放性能；后续可引入 pk manifest 或 bloom/filter。
3. 不改变用户 `write()` API。
4. 不处理跨逻辑分区的主键移动。金融时序表的主键通常包含时间列，修订行应落在同一逻辑时间分区。

## 设计

新增内部方法：

- `_pk_cases(row, pk)`：把一行转换为主键等值条件。
- `_find_existing_pk_segment(logical_part, row, pk, column_types)`：扫描当前逻辑分区下的物理 refs，返回包含该主键的 segment。
- `_insert_segment_batch(...)`：写入一个指定 segment 后刷新 manifest。

`_do_insert_segmented()` 改为按行处理需要全局主键语义的模式：

- `replace=True`：已有主键写回原 segment；新主键追加到 active segment。
- `ignore=True`：已有主键跳过；新主键追加到 active segment。
- `replace=False and ignore=False`：已有主键写回原 segment，让底层 SQLite 主键约束抛错；新主键追加到 active segment。

## 验证

1. 默认 `write()` 修订 sealed segment 中已有主键后，查询结果只保留新值。
2. `mode="ignore"` 对 sealed segment 中已有主键不产生新行。
3. 同批重复主键在 segment layout 下仍只有最后一次 `upsert` 结果。
4. 相关时序契约测试通过。
5. `python -m compileall -q iosql tests/sqlite/test_timeseries_contract.py` 通过。
6. `git diff --check` 通过。
