# PLAN-017-segment-global-primary-key-OUTCOME

## 结论

本轮 review 从金融数据重放和修订场景出发，确认 `PLAN-016` 后的 segment layout 存在逻辑主键一致性缺口：SQLite 主键约束只在单个 segment 文件内生效，默认 `write(mode="upsert")` 可能把同一逻辑主键写入新 segment，导致查询返回重复行。

本次已修复：segment writer 写入前会在当前逻辑分区的物理 refs 中按主键查找已有行。已有主键会按写入模式处理，不再绕过逻辑表主键。

## 已完成

1. 新增 `_pk_cases()`，把行数据转换为主键等值条件。
2. 新增 `_find_existing_pk_segment()`，在当前逻辑分区下扫描 legacy/segment 物理文件，定位已有主键所在 segment。
3. 新增 `_write_segment_rows()` 和 `_append_segment_rows()`，统一物理 segment 写入和 manifest 刷新。
4. `upsert` / `replace` 命中已有主键时写回原 segment，查询只保留新值。
5. `ignore` 命中已有主键时跳过，不创建新 segment。
6. `append` 命中已有主键时写回原 segment，由底层 SQLite 主键约束抛错，避免跨 segment 绕过唯一性。
7. 同批重复主键在默认 `upsert` 下按顺序处理，最后一次写入生效。
8. README 补充物理 segment 下写入模式仍遵守逻辑表主键语义。
9. 新增时序契约测试覆盖跨 segment upsert、ignore、append 和同批重复主键。

## 保留风险

1. 当前主键定位通过扫描当前逻辑分区的物理 refs 完成，优先保证正确性；大批量回放时性能仍可进一步优化。
2. 后续可增加 pk manifest 或 segment 级主键索引缓存，降低金融历史数据重放时的查找成本。
3. 跨逻辑分区的主键移动仍不做处理。金融时序表应把时间列放入主键，修订行应落在同一逻辑时间分区。

## 验证结果

已运行：

```bash
python -m pytest tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py -q
python -m pytest tests/sqlite -q
python -m compileall -q iosql tests/sqlite/test_timeseries_contract.py tests/query/test_text_where.py
git diff --check
```

结果：

- 时序契约和文本 where 测试：`22 passed`。
- SQLite 测试组：`16 passed, 11 skipped`。
- compileall 通过。
- diff check 通过。

pytest cache warning 来自 `/Users/mac/primates/.pytest_cache` 写权限，不影响测试结果。
