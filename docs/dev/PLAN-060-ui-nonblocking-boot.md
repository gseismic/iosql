# PLAN-060 · UI 非阻塞启动与渐进统计

## 问题

用户反馈 `iosql ui` "连接数据库要很长时间"。定位：

- `Database()` 构造是惰性的（0ms）；`/api/db` 2ms；单表 2000 万行
  `count_rows()` 热缓存仅 0.17s；100 表 × 8 segment（800 文件）
  全量统计热缓存 0.56s——本机 NVMe 热缓存均不慢。
- 真实慢因是**环境 × 架构放大**：HDD/冷页缓存/网络同步盘/杀毒扫描使
  每个物理文件首开代价高（数十 ms/文件），而当前启动契约要求
  `/api/tables` 对**每张表**串行做 spec + count_rows（逐 segment
  COUNT(*)）+ parts + segments 全量统计后，浏览器才离开
  "正在连接数据库…" 启动屏。文件越多/盘越慢，白屏时间线性放大。
- 且行数 TTL 缓存仅 60s，每次刷新页面都重付一次全量统计成本。

结论：不需要"另一个 web 版本"（UI 本身就是本地 Web 应用），
需要把**启动契约从阻塞式全量统计改为非阻塞渐进统计**。

## 变更

### 后端（iosql/ui/app.py）
1. `GET /api/tables` 默认轻量化：只返回 manifest 级元数据
   （name/partition_str/has_time/columns/pk/order_key），不做逐表
   count/parts/segments I/O；`?with_stats=1` 保留旧行为（兼容脚本）。
2. 新增 `GET /api/table/{name}/stats`：单表
   {row_count, part_count, segment_count}，行数沿用 60s TTL 缓存。
3. `/api/tables` 内部 `db.table_names()` 提升为一次调用（原每表一次）。

### 前端（static/）
4. app.js：boot 只等 `/api/db` + 轻量 `/api/tables` 即渲染；
   渲染后以并发 4 逐表拉取 stats，写入 `IU.statsCache` 并刷新
   所有 `[data-stats-for]` 单元格与总行数卡。
5. 侧栏行数、Overview 表格行数/分区数/segment 数单元格渲染为
   占位 "…"，stats 到达后就地更新（不整页重渲染）。

### 测试（tests/test_ui.py）
6. 轻量 tables 无 row_count 字段且耗时不受大表影响（shape 断言）；
   with_stats=1 兼容旧契约；stats 端点数值正确；既有用例迁移。

### 文档
7. PLAN-060 + OUTCOME；skills/iosql-usage ui 段补一句启动行为。

## 验收
- 大库（数百文件）打开 UI：侧栏与 Overview 秒出，计数异步填充。
- 既有 39 项 UI 测试全绿；新增用例通过。
