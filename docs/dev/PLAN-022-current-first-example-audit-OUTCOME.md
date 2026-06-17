# PLAN-022-current-first-example-audit-OUTCOME

## 结论

本轮以当前 `master` 的时序表契约、物理 segment 和查询 API 为准，只吸收旧目录里确定有用的示例意图。

`iosql_mac` 没有独有提交，只是停在 `15efef6` 的旧 checkout。分区相关实现和测试不需要从旧目录恢复；当前实现已经比旧目录更完整，包括：

1. `CalendarPartition.get_range()` 使用配置的时区解析。
2. `DurationPartition` 支持更多 interval 别名。
3. 当前 `timeseries` 写入会使用 `TableSpec` 和 `PhysicalLayout`，并维护 segment manifest。
4. 当前查询计划已经能展示逻辑分区和物理 segment 的裁剪结果。

旧分区 demo 的可借鉴点是“规模写入 + 窗口查询 + 布局观测”的演示目标，而不是旧式 `insert_all(..., partition=...)` API。

## 已完成

1. 新增 `examples/05_partition_segments_smoke.py`
   - 使用当前 `Database.timeseries()`。
   - 写入 6 个逻辑日分区、288 行数据。
   - 用较低 `max_rows_per_file` 触发 12 个物理 segment。
   - 输出 `layout_report()`、`segments()` 和 `query().explain()` 的关键信息。
2. 恢复精简后的 `examples/mysqlite/`：
   - `01_schemaless_crud.py`
   - `02_bulk_query_and_conflict.py`
   - `03_connection_pool_cleanup.py`
3. 重写 `iosql/utils/mysqlite/README.md`
   - 替换旧 changelog。
   - 说明它是低层 SQLite 工具层，不是新的主 API。
   - 补充自动建表、动态加列、批量写入、查询、冲突处理和连接池用法。
4. 更新 `examples/README.md` 和根 `README.md`
   - 加入新的分区/segment 示例。
   - 标注 `examples/mysqlite/` 是低层 API 示例入口。

## 未采纳

1. 未恢复旧 `examples/01_duration_ingest.py`、`02_calendar_schedule.py`、`03_composite_quotes.py`，当前主示例已经用 `timeseries` API 覆盖对应路径。
2. 未恢复旧 `examples/04_large_partition_demo.py` 原始实现，避免继续传播旧式 `DurationPartition` 直传写法。
3. 未恢复全部旧 mysqlite 示例，避免示例体系重新膨胀。
4. 未使用旧 `pyproject.toml`、未跟踪 skill 和运行产物。

## 验证结果

已运行新增示例：

```bash
python examples/05_partition_segments_smoke.py
python examples/mysqlite/01_schemaless_crud.py
python examples/mysqlite/02_bulk_query_and_conflict.py
python examples/mysqlite/03_connection_pool_cleanup.py
```

分区示例输出确认：

- 写入 `288` 行。
- 生成 `6` 个逻辑分区。
- 生成 `12` 个物理 segment。
- 时间窗口查询只扫描目标日的 `2` 个 segment。

已运行测试和静态检查：

```bash
python -m pytest tests/sqlite/test_timeseries_contract.py tests/utils/mysqlite -q
python -m compileall -q iosql examples tests/sqlite/test_timeseries_contract.py tests/utils/mysqlite
git diff --check
```

结果：

- `60 passed`。
- compileall 通过。
- diff check 通过。
- pytest cache warning 来自父目录 `/home/lsl/macbook/primates/.pytest_cache` 只读，不影响测试结果。
