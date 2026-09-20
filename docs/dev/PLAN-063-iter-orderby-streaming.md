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

1. **连接生命周期（v3）：专用只读连接 + 每 part 单游标，无 OFFSET，失败不静默。**
   初版 A（单连接长游标）与 `max_open_partitions`（默认 5）冲突致自旋死锁；改版 B（每页
   open/close + LIMIT/OFFSET）规避了死锁，但被评审否决三项：内存实为 O(分区数 × 内部页)、
   每页重跑 ORDER BY（temp_store=memory 下临时 B 树驻 RAM 且重复排序）、OFFSET 分页在并发
   写下重读/漏行。v3 定稿：`_open_part_read_conn` 以 `mode=ro` URI 打开专用只读连接
   （WAL 边缘场景回退普通打开，isfile 前置保证绝不创建缺失文件；`query_only`、
   `cache_size=-1024`、`temp_store=FILE`）；连接/初始化失败一律抛 `UnsafeOperation`
   （带 part_dbfile），不做静默空分区；仅已成功连接后的 `no such table` 兼容为空流。
   每 part 一条连接一个游标流式 fetchmany——排序每 part 一次；游标存活期间语句级读快照
   （part 内不重不漏）；专用连接绕开连接池，不受 `max_open_partitions` 约束。
2. **连接数预算（v3）**：`max_ordered_read_connections`（默认 128，Database 级配置）；
   归并连接数超上限直接抛 `BadArgument` 快速失败（不做信号量——k 路归并会互相等待死锁），
   错误信息给出三条修复路径。默认上限下 chunk 下限 64 不会触底，驻留钳制声明成立；
   外部归并（波次 + 临时排序文件）列为后续 TODO。
3. **limit 下推 per-part LIMIT**：全局 limit 直接编入每 part 的 SQL（`LIMIT ?`），
   SQLite top-N 排序器排序内存 O(limit)；实测多分区 limit=1 归并 Python 峰值 218 KB。
4. **P 自适应 fetch 粒度**：`_merge_stream_chunk(batch_size, P) = max(64, min(batch_size, 100_000 // P))`，
   Python 峰值驻留 ≈ P × chunk，总量钳在 ~10 万行（由连接数上限保证公式成立）。
5. **归并排序键 `_SqliteSortKey`（模块级）**：对齐 SQLite 值序
   （NULL→数值→文本→blob→其他 rank；ASC NULL 最小 / DESC NULL 最大，方向内联含秩反转）。
   首版缺陷（DESC 未反转秩比较导致 NULL 排首）已在实施中修复并由测试锁定。
   不可比较类型的兜底：同秩内 TypeError → str 比较（与 `_apply_order_by` 的兜底方向一致）。
6. **tie 顺序**：`heapq.merge` 相等键优先早 part，与 `select()` 的 part 顺序 + Python 稳定排序一致。
7. **快照语义（如实声明）**：流建立后本迭代不可见新增 part/行（refs 与快照统一）；
   part 内为语句级读快照，跨 part 非原子；WAL 下长游标不阻塞写入。
8. **混入改动回退**：他人错误混入的 segment stats `nulls` 计数（无消费方、扩 manifest 契约、
   与本计划无关）已回退；其遗留的两份 competing 设计/计划文档一并移除（见 OUTCOME）。

## 实施点（as-built, v3）

1. `_select_queries_for_where(..., order_by_sql=None, limit=None)`：透传给两个 query builder
   （均已支持 order_by_sql/limit）。
2. `_open_part_read_conn(part_dbfile)`：专用只读连接（mode=ro URI 优先、row_factory=Row、
   query_only=ON、temp_store=FILE、cache_size=-1024），绕开分区连接池；
   文件缺失/打不开/初始化失败 → `UnsafeOperation`（带 part_dbfile）。
3. `_part_ordered_rows(part_dbfile, query, chunk_size)`：单游标 fetchmany 行流；
   `no such table` → 空流（与无序路径 NoSuchTable 跳过行为一致）；其余错误上抛。
4. `_iter_select_ordered_batches(...)`：
   - meta/column_types/fetch_fields/project_fields 与 `select()` 同一套 helper；
   - per-part LIMIT 下推（全局 limit 编入每 part SQL）；
   - 先收集全部 (ref, query)，连接数超 `max_ordered_read_connections` 快速失败；
   - chunk 按 P 自适应；`heapq.merge(*streams, key=_row_sort_key(specs))`；
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
4. `test_iter_select_batches_order_by_concurrent_write_snapshot`：迭代中并发写入
   （评审复现场景：插入更小排序值），不重不漏、新行不可见（快照语义）。
5. `test_iter_select_batches_order_by_limit_pushdown_many_parts`：多分区 limit=1 归并出
   1 行且与 `select()` 一致；per-part `LIMIT ?` 结构锁定。
6. `test_iter_select_batches_order_by_part_conn_failure_fails`：单分区连接失败
   （chmod 000）查询必须失败，错误信息可定位分区文件（评审 P1-1）。
7. `test_iter_select_batches_order_by_over_conn_cap_fails`：连接数超
   `max_ordered_read_connections` 快速失败；无 order_by 路径不受影响（评审 P1-2）。
8. 回归：既有 `test_iter_select_batches_streams_rows_and_honors_limit` 继续通过。

## 产出

- `docs/dev/PLAN-063-iter-orderby-streaming-OUTCOME.md`
