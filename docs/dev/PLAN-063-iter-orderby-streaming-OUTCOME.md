# PLAN-063 OUTCOME: iter_select 带 order_by 流式化

结果：已完成（经两轮评审修订，当前为 v2 实现）。SQLite 引擎带 `order_by` 的流式查询不再走
`select()` 全量物化，改为跨 partition 的 k 路归并流式读取；UI export 随之放开 `order_by`。

## 评审意见回应

> iosql 暴露了 iter_rows/iter_batches；但当前 iosql 实现只要带 order_by="open_time"，
> 就会走 select() 的全量结果路径。

确认属实（`iter_select_batches` 的 order_by 分支直接调 `select()`），已修复：
- `iter_select` / `iter_select_batches` / `query().iter_rows()` / `query().iter_batches()` 带
  order_by 时走 `_iter_select_ordered_batches`：每 part 一条专用只读连接单游标（无 OFFSET），
  `heapq.merge` 归并，limit 归并侧提前终止；
- 回归测试 `test_iter_select_batches_order_by_streams_without_full_select` 通过
  monkeypatch `select()` 抛错的方式永久锁定"不许回退全量路径"。

## 第二轮评审（提交 1af0c76）三项发现与解决

| # | 发现 | 核实 | 解决（v2） | 实测 |
| --- | --- | --- | --- | --- |
| 1 | 内存实为 O(分区数 × 内部页)，limit=1 也每分区预取一页 | 属实（chunk 下限 1 万 + merge 起步预取 + `list(rows)` 复制） | P 自适应小 chunk（≤10 万行总量钳制），limit 下推 per-part LIMIT，去掉 list 复制 | 26 分区 limit=1：py 峰值 218 KB；全量起始峰值 5.2 MB |
| 2 | ORDER BY+LIMIT/OFFSET 每 part/每页重排序；temp_store=memory 下临时 B 树驻 RAM | 属实（EQP: USE TEMP B-TREE；每页重跑） | 每 part 单 SQL 单游标（排序一次、无 OFFSET 重复）；专用连接 temp_store=FILE 可落盘、cache_size=-1024；limit 走 top-N 排序器 | PRAGMA temp_store=1(FILE)；EQP 单次 |
| 3 | OFFSET 分页期间并发写入重读/漏行 | 属实（评审复现 -1 插入场景） | 无 OFFSET；游标存活期间语句级读快照，refs 建流后固定（新增 part/行不可见） | 迭代中 insert seq=-1/990000：无重无漏、新行不可见、与迭代前快照逐行一致 |

快照语义如实写进 README：分区文件内语句级读快照，跨分区非原子。

## 第三轮评审（提交 1d32058）两项发现与解决

| # | 发现 | 核实 | 解决（v3） |
| --- | --- | --- | --- |
| 1 | 分区连接失败被静默当成空分区（捕获过宽 → None → 空流），fd 耗尽/权限/锁/损坏时导出 200 但缺数据 | 属实 | `_open_part_read_conn` 改为 `mode=ro` URI 打开（绝不创建缺失文件，WAL 边缘场景回退普通打开但仍 isfile 前置 + query_only）；连接/初始化失败一律抛 `UnsafeOperation`（带 part_dbfile 与底层错误）；仅已成功连接后的 `no such table` 兼容为空流；新增测试锁定"单分区连接失败查询必须失败" |
| 2 | raw 连接绕开连接池，max_open_partitions/max_connections_per_file/max_process_connections 对排序流全部失效；1000 分区 limit=1 仍开 ~1000 连接；chunk 下限 64 使 P>1562 时驻留重新超 10 万行 | 属实 | 新增 `max_ordered_read_connections`（默认 128，Database 级配置，与既有三参数同模式透传）；归并连接数超上限直接抛 `BadArgument` 快速失败（不做信号量——k 路归并会互相等待死锁），错误信息给出三条修复路径；默认上限 128 下 chunk 下限 64 不会触底（100_000//128=781），驻留钳制声明成立。外部归并/临时排序文件方案列为后续 TODO |

文档修正：OUTCOME 重复标题"评审意见回应"去重；测试统计更正为 676 passed, 16 skipped。

## 实施中的两次设计修正

1. **连接生命周期（v1→v2）**：初版"单连接单游标跨页持有"与 `max_open_partitions`（默认 5）冲突
   （lease 阻止淘汰 → `_acquire_part_db` 永久自旋，测试挂死复现）；第一轮修订为"每页
   open/close + LIMIT/OFFSET"，随后被第二轮评审否决（上表 3 项）——OFFSET 是方向性错误。
   v2 定稿为专用只读连接（raw sqlite3，绕开连接池）+ 单游标，兼得快照一致性、无重复排序、
   以及不受 `max_open_partitions` 约束。
2. **比较器 DESC 秩反转**：`_SqliteSortKey` 首版 DESC 分支未反转 rank 比较，
   导致 NULL 在 DESC 排首（与 SQLite 及 `select()` 的 NULL 最后语义相悖）。已修复并由
   `test_iter_select_batches_order_by_limit_project` 的 NULL 断言锁定。

## 变更清单（v3 后累计）

- `iosql/engines/sqlite/table.py`
  - `+import heapq`/`+import pathlib`；新增模块级 `_sqlite_sort_rank` / `_SqliteSortKey`；
  - `_select_queries_for_where` 增加 `order_by_sql`/`limit` 透传；
  - 新增 `_open_part_read_conn`（mode=ro，失败抛 UnsafeOperation）/
    `_part_ordered_rows` / `_iter_select_ordered_batches` / `_row_sort_key`
    / `_merge_stream_chunk`；连接数上限检查；
  - `iter_select_batches` 的 order_by 分支改调流式归并路径。
- `iosql/database.py`：`+max_ordered_read_connections`（默认 128）配置与透传。
- `iosql/ui/app.py`：export 端点移除 order_by 拒绝（引擎流式化后理由失效），透传给引擎。
- `tests/engines/sqlite/test_partitioned_table_contract.py`：+7 测试。
- `tests/test_ui.py`：+1 测试 `test_export_order_by_streams_sorted`。
- `README.md`：排序导出的内存公式、快照语义、值序语义、与 `select()` 一致性。
- 回退他人错误混入的 segment stats `nulls` 改动（无消费方、扩 manifest 契约、与本计划无关）；
  移除其遗留的 competing 文档 `docs/dev/PLAN-063-streaming-ordered-iteration.md`、
  `docs/design/streaming-ordered-20260920-overview.md`（PLAN-063 编号唯一化）。

## 验证

- `python -m pytest tests/engines/sqlite/test_partitioned_table_contract.py -q`：24 passed。
- `python -m pytest tests/test_ui.py -q`：45 passed。
- 全量 `python -m pytest tests/ -q --ignore=tests/01_large_partition.py`：678 passed, 16 skipped。
- 专项实测见"第二轮评审"表（26 分区 × limit=1 / 全量起始 / 快照一致性 / EQP / PRAGMA）。

## 已知边界 / 后续

- **分区数超上限的有序归并**：当前快速失败（`max_ordered_read_connections`，默认 128）；
  后续可实施外部归并（波次归并 + 临时排序文件），消除该上限并保证任意分区数。
- 无索引 + 无 limit 时每 part 仍要完整排序一次（SQLite sorter，temp_store=FILE 落盘，
  RAM 有界但 CPU O(n log n)）；对排序热点列可用 `create_index()` 让 ORDER BY 走索引消除排序。
- 快照跨分区非原子：迭代期间另一连接提交的写入可能在"未开游标的分区"可见——本实现中所有
  游标在首个归并行产出前统一建立，实际窗口极小，但严格全库一致读需要外部锁定，未提供。
- `select()` 无 limit 时仍为急切全量（文档契约如此），未在本计划范围。
- 混合类型列的排序兜底：归并路径为 SQLite rank 序，与 `select()` 纯 Python 兜底（str 比较）
  在病态数据下可能不同；写入侧类型校验使该场景基本不出现，未做统一。
