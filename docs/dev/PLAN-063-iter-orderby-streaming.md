# PLAN-063: iter_select 带 order_by 时流式化（消除全量物化退化）

## 背景 / 问题

外部评审意见：iosql 暴露了 `iter_rows`/`iter_batches` 流式读取能力；但 SQLite 引擎的
`iter_select_batches()` 只要带 `order_by="open_time"` 就退化为 `select()` 的全量结果路径
（`engines/sqlite/table.py:2846-2857`：`if order_by: rows = self.select(...)` 后再切片分批）。

影响链路：
- `table.query(...).iter_rows()` / `.iter_batches()`（`_TableQuery`）→ `iter_select(_batches)` → 带 order_by 时全量物化。
- 大表流式导出场景（如 tick/orderbook），内存随结果集无界增长，违背 `iter_*` 的流式契约。

对照：PG 引擎用命名服务端游标流式执行 `ORDER BY`，无此问题（`engines/postgresql/table.py:973-1009`）。

## 目标

SQLite 引擎 `iter_select_batches` 带 `order_by` 时改为**有界内存的流式 k 路归并**：
- 每 part（segment 文件）用 `ORDER BY + LIMIT/OFFSET` 分页流式读取；
- 跨 part 用 `heapq.merge` 归并，峰值驻留 = 单个内部页（≤10 万行）+ 归并堆（每 part 1 行），与结果集总量 N 解耦；
- `limit` 归并侧提前终止；`fields` 投影、`to_dict`、`batch_size` 语义不变；
- 结果与 `select(order_by=...)` 一致（纯类型列下逐行相等）。

## 关键设计决策（实施过程中修订）

1. **连接生命周期：每页 open/close，不跨页持有。**
   初稿曾设计"单连接单游标流式 fetchmany"，实测与 `max_open_partitions`（默认 5）冲突：
   k 路归并要求同时打开所有 part 连接，lease 阻止淘汰 → `_acquire_part_db` 永久自旋（测试挂死复现）。
   修正为与既有 `_iter_select_raw_batches` 一致的"每次 open/close 一个分区连接"，任一时刻至多 1 个。
   排序成本代价：每页 ORDER BY 重排（O(pages × n log n)），用大内部页摊薄：
   `_ordered_fetch_chunk(batch_size) = min(100_000, max(batch_size, 10_000))`。

2. **归并排序键 `_SqliteSortKey`（模块级）**：对齐 SQLite 值序
   （NULL→数值→文本→blob→其他 rank；ASC NULL 最小 / DESC NULL 最大，方向内联含秩反转）。
   首版缺陷（DESC 未反转秩比较导致 NULL 排首）已在实施中修复并由测试锁定。
   不可比较类型的兜底：同秩内 TypeError → str 比较（与 `_apply_order_by` 的兜底方向一致）。

3. **tie 顺序**：`heapq.merge` 相等键优先早 part，与 `select()` 的 part 顺序 + Python 稳定排序一致。

4. **混入改动回退**：他人错误混入的 segment stats `nulls` 计数（无消费方、扩 manifest 契约、
   与本计划无关）已回退；其遗留的两份 competing 设计/计划文档一并移除（见 OUTCOME）。

## 实施点（as-built）

1. `_select_queries_for_where(..., order_by_sql=None)`：透传给两个 query builder（均已支持）。
2. `_iter_part_ordered_batches(part_dbfile, query, chunk_size)`：
   `ORDER BY + LIMIT ? OFFSET ?` 分页，每页 open/close 分区连接；
   `no such table`（裸 OperationalError）→ 空流（与无序路径 NoSuchTable 跳过行为一致）。
3. `_part_ordered_rows(...)`：批次摊平为行流。
4. `_iter_select_ordered_batches(...)`：
   - meta/column_types/fetch_fields/project_fields 与 `select()` 同一套 helper；
   - 先收集全部 (ref, query) 再统一建流（chunk 与 P 无关，均匀）；
   - `heapq.merge(*streams, key=_row_sort_key(specs))`；
   - 归并侧：投影/`to_dict` 归一、limit 提前 break、攒批 batch_size yield。
5. `iter_select_batches` 的 `if order_by:` 分支改调新方法；`select()` 急切契约不变；PG 引擎不动。

## UI 导出（超出原计划的连带修正）

`ui/app.py` export 端点原以"引擎带 order_by 会全量物化"为由拒绝 `order_by`；引擎流式化后该理由
失效，改为透传 `order_by`（BadArgument → 422 映射与预取校验模式保证错误在响应头前暴露），
并新增测试 `test_export_order_by_streams_sorted` 锁定有序流式导出。

## 测试（tests/engines/sqlite/test_partitioned_table_contract.py）

1. `test_iter_select_batches_order_by_streams_without_full_select`：monkeypatch `Table.select`
   抛错，带 order_by 的 `iter_select_batches` 仍产出（回归：不再走全量路径）。
2. `test_iter_select_batches_order_by_matches_select_multi_part`：多分区 + 乱序写入，
   asc/desc/多列混合方向三种 order_by 与 `select()` 逐行相等。
3. `test_iter_select_batches_order_by_limit_project`：投影不含排序列时输出仍按其有序；
   limit 截断与批边界正确；`to_dict=False` 返回 tuple；NULL 排序值 DESC 排最后且与 `select()` 一致。
4. 回归：既有 `test_iter_select_batches_streams_rows_and_honors_limit` 继续通过；
   全量套件 673+ 无回归。

## 产出

- `docs/dev/PLAN-063-iter-orderby-streaming-OUTCOME.md`
