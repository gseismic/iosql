# PLAN-063 OUTCOME: iter_select 带 order_by 流式化

结果：已完成。SQLite 引擎带 `order_by` 的流式查询不再走 `select()` 全量物化，
改为跨 partition 的 k 路归并流式读取；UI export 随之放开 `order_by`。

## 评审意见回应

> iosql 暴露了 iter_rows/iter_batches；但当前 iosql 实现只要带 order_by="open_time"，
> 就会走 select() 的全量结果路径。

确认属实（`iter_select_batches` 的 order_by 分支直接调 `select()`），已修复：
- `iter_select` / `iter_select_batches` / `query().iter_rows()` / `query().iter_batches()` 带
  order_by 时走 `_iter_select_ordered_batches`：每 segment 按 `ORDER BY + LIMIT/OFFSET`
  分页流式读取，`heapq.merge` 归并，limit 归并侧提前终止；
- 内存从 O(结果集) 降为 O(单内部页 ≤10 万行 + 归并堆每 part 1 行)；
- 回归测试 `test_iter_select_batches_order_by_streams_without_full_select` 通过
  monkeypatch `select()` 抛错的方式永久锁定"不许回退全量路径"。

## 实施中的两次设计修正

1. **连接生命周期**：初版"单连接单游标跨页持有"与 `max_open_partitions`（默认 5）冲突，
   多 part 归并时 `_acquire_part_db` 永久自旋（测试挂死复现）。改为每页 open/close，
   任一时刻至多 1 个分区连接；ORDER BY 每页重排的成本用大内部页
   （`min(100_000, max(batch_size, 10_000))`）摊薄。
2. **比较器 DESC 秩反转**：`_SqliteSortKey` 首版 DESC 分支未反转 rank 比较，
   导致 NULL 在 DESC 排首（与 SQLite 及 `select()` 的 NULL 最后语义相悖）。已修复并由
   `test_iter_select_batches_order_by_limit_project` 的 NULL 断言锁定。

## 变更清单

- `iosql/engines/sqlite/table.py`
  - `+import heapq`；新增模块级 `_sqlite_sort_rank` / `_SqliteSortKey`；
  - `_select_queries_for_where` 增加 `order_by_sql` 透传；
  - 新增 `_iter_part_ordered_batches` / `_part_ordered_rows` / `_iter_select_ordered_batches`
    / `_row_sort_key` / `_ordered_fetch_chunk`；
  - `iter_select_batches` 的 order_by 分支改调流式归并路径。
- `iosql/ui/app.py`：export 端点移除 order_by 拒绝（引擎流式化后理由失效），透传给引擎。
- `tests/engines/sqlite/test_partitioned_table_contract.py`：+3 测试（见计划）。
- `tests/test_ui.py`：+1 测试 `test_export_order_by_streams_sorted`。
- `README.md`：注意事项更新——排序导出内存有界、值序语义、与 `select()` 一致性。
- 回退他人错误混入的 segment stats `nulls` 改动（无消费方、扩 manifest 契约、与本计划无关）；
  移除其遗留的 competing 文档 `docs/dev/PLAN-063-streaming-ordered-iteration.md`、
  `docs/design/streaming-ordered-20260920-overview.md`（PLAN-063 编号唯一化）。

## 验证

- `python -m pytest tests/engines/sqlite/test_partitioned_table_contract.py -q`：20 passed。
- `python -m pytest tests/test_ui.py -q`：45 passed。
- 全量 `python -m pytest tests/ -q --ignore=tests/01_large_partition.py`：673 passed, 16 skipped。

## 已知边界 / 后续

- 有序分页在无索引时每页触发该 segment 的 ORDER BY 重排；内部页默认 ≥1 万行摊薄，
  极端大 segment + 极小 batch_size 时有放大效应，属可接受权衡（与正确性/内存有界优先级一致）。
- `select()` 无 limit 时仍为急切全量（文档契约如此），未在本计划范围。
- 混合类型列的排序兜底：归并路径为 SQLite rank 序，与 `select()` 纯 Python 兜底（str 比较）
  在病态数据下可能不同；写入侧类型校验使该场景基本不出现，未做统一。
