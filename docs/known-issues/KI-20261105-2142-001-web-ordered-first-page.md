# KI-20261105-2142-001：SQLite 大表有序首页仍需查询全部候选分区

- 创建时间：2026-11-05 21:42（Asia/Shanghai）。
- 模型：Codex，环境说明其基于 GPT-6；未提供可核实的完整模型 ID。
- 初始状态：未解决。
- 来源：用户反馈 `iosql web` 进入大表 Data 加载慢；本次实现按需统计时复核剩余查询成本。
- 确认版本：`WORKTREE`，基准提交 `47b8c796945aad7a710f11b93c77f88c5a2b6c00`；相关未提交文件包括 `iosql/web/static/app.js`、`iosql/web/static/js/tab-data.js`、`iosql/web/app.py` 和本次计划。工作区尚未固定为提交。引入提交未知。

## 2026-11-05 21:42 确认

对未开启物理 segment 且分区列属于主键的 SQLite 表，无筛选的 Data 首页先探测排序列是否含 NULL，再读取有序首页（`iosql/web/app.py:725`、`iosql/web/app.py:772`）。SQLite 将排序和 limit 下推到各物理文件，但 `_select_and_cases` 会遍历全部候选文件并分别执行查询（`iosql/engines/sqlite/table.py:3817`、`iosql/engines/sqlite/table.py:4256`）。条件能够裁剪分区时，候选范围可以缩小；是否扫描文件内大量行取决于索引和查询计划。

影响：分区很多、冷缓存或排序缺少合适索引时，即使只显示一页，首屏仍可能较慢。历史实测记录 `docs/dev/PLAN-069-fast-first-page-OUTCOME.md` 中的 839 MB、13 段样本冷进程首页约 5.2 秒；它不是本次用户库的冷启动测量。本次用户库临时副本在暖缓存下，从总览进入 Data 显示 200 行约 0.46 秒，未测冷缓存。

本次按需加载移除了默认的行数与 segment 统计请求，不能消除有序首页查询本身的成本。临时绕过：通过表头对时间或分区列筛选，缩小可裁剪的范围；也可按实际查询计划评估索引。不得仅依据当前分区名称假定历史布局正确而提前停止，否则可能漏行；NULL 检测和全局排序语义需保留。

关联计划：`docs/plans/PLAN-20261105-007-model-unknown-web-lazy-loading.md`。
