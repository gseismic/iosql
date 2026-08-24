# PLAN-060 · 结果记录（OUTCOME）

> 计划：PLAN-060-ui-nonblocking-boot.md

## 状态：COMPLETE（2026-08-25）

## 问题定位（用户反馈"连接数据库要很长时间"）

逐层计时排除了常见嫌疑：

| 阶段 | 耗时（本机 NVMe 热缓存） |
|---|---|
| `Database()` 构造 | 0ms（纯惰性，无 I/O） |
| `GET /api/db` | 2ms |
| `count_rows()` 2000 万行单表 | 0.17s |
| 100 表 × 8 segment（800 文件）全量统计 | 0.56s |
| `/api/table/{name}/stats` 单表 | 15-20ms |

结论：慢因是**环境 × 架构放大**——HDD/冷页缓存/网络同步盘/杀毒扫描
使每个物理文件首开代价高，而旧启动契约要求 `/api/tables` 对每张表
串行完成 count_rows（逐 segment COUNT(*)）+ parts + segments 后
浏览器才离开"正在连接数据库…"启动屏；文件越多、盘越慢，白屏线性放大。
且行数 TTL 仅 60s，每次刷新重付全量成本。

**回答"iosql web 是否更好"**：UI 本身就是本地 Web 应用（FastAPI +
浏览器），不存在更快的另一个"web 版本"；需要修的是启动契约，
不是架构。

## 交付内容

### 后端（iosql/ui/app.py）
- `GET /api/tables` 默认轻量化：仅 manifest 级元数据
  （name/partition_str/has_time/columns/pk/order_key），不碰物理文件；
  `?with_stats=1` 保留旧阻塞行为（脚本/测试兼容）。
- 新增 `GET /api/table/{name}/stats`：单表统计，行数沿用 60s TTL 缓存。
- `/api/tables` 内部 `db.table_names()` 从每表一次提升为一次调用。

### 前端（static/）
- app.js：boot 只等 `/api/db` + 轻量 tables（毫秒级）即渲染；
  `pumpStats()` 并发 4 逐表拉取 stats 写入 `state.stats`，
  `refreshStatsViews()` 就地更新所有 `[data-stats-for]` 单元格与
  总行数卡（含"统计中…"提示与占位 title 清理）。
- 侧栏行数、Overview 行数/分区数/segment 数渲染占位 "…"，
  stats 到达后就地填充，不整页重渲染。
- tab-overview.js：统计单元格改带 data 属性；总行数卡支持渐进求和。

## 验证

- pytest：**403 passed, 16 skipped**（含新增
  `test_list_tables_light_default`、`test_table_stats_endpoint`，
  旧 summary 用例迁移到 `with_stats=1`）。
- Playwright 大库冒烟（100 表 × 8 segment = 800 文件）：
  - 侧栏首屏 **0.27s**（旧契约下需等全量统计完成后才渲染）
  - 100 表统计全部填充 **1.49s**（并发 4）
  - 总行数 1,500,000 正确，hint 清空，截图人工核对通过。

## Review 记录

自审修复 2 项：
1. `statCard(label, value)` 只收一个值，总行数卡的"统计中"提示
   span 作为第三参数被静默丢弃 → 包一层容器修正。
2. `refreshStatsViews` 只更新文本未清占位 `title`，导致外部轮询
   （及依赖该标记的逻辑）永不收敛 → 数值填充后移除 title。

## 遗留与后续

- 若用户环境（HDD/网络盘）单表 stats 仍慢，可考虑把行数缓存 TTL
  提升为可配置，或后台预热；当前 60s TTL 在交互场景下够用。
- `/api/tables?with_stats=1` 为兼容保留，未来脚本应迁移到 `/stats`。
