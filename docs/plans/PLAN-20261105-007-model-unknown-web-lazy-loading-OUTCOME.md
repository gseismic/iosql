# iosql Web 大表按需加载结果

- 完成时间：2026-11-05 21:42（Asia/Shanghai）。
- 执行模型：环境说明为 GPT-6，完整模型 ID 未提供，按规范使用 `model-unknown`。
- 结果版本：`WORKTREE`，基准提交 `47b8c796945aad7a710f11b93c77f88c5a2b6c00`。本次继续修改已有 PLAN-006 的未提交 Web 前端、README、使用说明及浏览器测试；新增本计划、结果和已知问题记录，更新计划与问题索引。结果尚未提交。
- 计划：`docs/plans/PLAN-20261105-007-model-unknown-web-lazy-loading.md`。

## 完成情况

- 进入 Overview 或 Data 不再自动发起 `/stats`、`/segments`、`/count`。总览提供单表或全部表统计以及单表分布加载，明确区分未统计、进行中及失败并支持重试。
- Data 默认只拉当前页；当前筛选结果的准确行数通过「统计总行数」按需获取，按条件缓存，刷新曾统计的条件时重新计数；切换查询或离开时取消浏览器在途请求并丢弃过期结果，已在服务端启动的查询可能继续执行。分页、筛选、排序和 NULL 完整性检查保持原有语义。
- Structure 在进入后获取分区与 segment 明细，分布与物理明细共享请求；重新进入或切换 obsolete 状态会刷新，并在请求失败后同步两处重试状态。
- 更新说明及静态资源版本；保留既有用户界面改动，未修改服务端数据接口及数据库文件。

## 验证

- `python -m pytest tests/test_web_browser.py -q`：18 passed，2 个依赖弃用警告。覆盖启动及分页无隐式统计、按需计数和失败重试、过期响应、Structure 共享请求及再次进入刷新等。
- `python -m pytest tests/test_web.py tests/test_plan062_web.py tests/test_plan072_fixes.py -q`：95 passed。
- 修改的 JavaScript 文件通过 `node --check`；`git diff --check` 通过。
- 对用户提供的 `kline-test.sqlite-6h.iosql` 创建临时副本并启动独立只读服务：暖缓存下从 Overview 点击进入 Data 并显示 200 行约 0.462 秒，浏览器无页面错误。网络请求依次为 `/api/db`、`/api/tables`、`/spec`、`/rows`，没有请求统计或 segment 明细。复制会预热缓存；不能将此数据解释为冷启动耗时或生产环境提速比例。临时副本与服务已清理。
- 用户原先的 8686 服务在实测前已停止；没有重启或修改原服务和原库。启动原命令并刷新页面后可使用本次前端变更。

## 偏差与遗留问题

没有改变服务端查询或分页接口。无筛选的 SQLite 有序首页仍需查询全部候选分区，冷缓存、缺少适当索引或大量分区时可能慢；完整性约束使仅按分区名提前停止并不安全。已记录 `KI-20261105-2142-001`（`docs/known-issues/KI-20261105-2142-001-web-ordered-first-page.md`）。

## 2026-11-05 22:07：原库手动浏览器测试及修复

执行者：Codex，环境说明其基于 GPT-6，完整模型 ID 未提供。结果为 `WORKTREE`，基准提交仍为 `47b8c796945aad7a710f11b93c77f88c5a2b6c00`。

使用用户指定的 `/media/lsl/759a7b09-1bc0-4e8c-8665-053892eb3cc9/finbase.crypto/binance/future_usdt/kline-test.sqlite-6h.iosql` 启动只读 Web 服务，使用有界面 Chromium 逐步操作，而非只检查临时副本：

- 从 Overview 进入 Data，看到 200 行；首次 `/rows` 在当时缓存状态下约 199ms。首屏请求仅包含 `/api/db`、`/api/tables`、`/spec`、`/rows`，没有自动计数或统计请求；该时间不代表冷启动基准。
- 前后翻页无重复；筛选 BTCUSDT 后按需计数并翻页，不重复发起计数。切换工作区后恢复筛选，复制链接在新标签页打开可复现表头筛选。
- Structure 正确显示 6 个分区；对 `6hour` 首分区点击后，BTCUSDT 的 18 行均处于左闭右开区间 `[1793772000000, 1793793600000)`，显式计数为 18，新标签页复现相同条件和行数。Overview 的分布和行数统计分别在操作后请求。
- 在 901px 及 390px 视口检查状态栏和侧栏抽屉；手机宽度切换到每页 100 行正常。浏览器控制台无错误。测试只调用读取接口，未通过 Web 修改原库；库在测试期间可能由其他进程更新。

手测发现并修复：计数进行中/成功时多余的 `null`；未统计状态的重复分隔符和中等窗口底部栏换行；同表切换后表头筛选 URL 丢失；合法字符串周期 `6hour` 的分区无法跳转。Data 的视图参数同步写入 URL，保留已有同表条件叠加语义；高级条件、排序和页位不纳入 URL。固定时长解析对未知或无法安全表示的值继续禁用分区链接。

最终验证：`tests/test_web_browser.py` **36 passed**，包含人工发现问题的回归、不同宽度/字号、分区间隔合法性及分享链接；`tests/test_web.py tests/test_plan062_web.py tests/test_plan072_fixes.py` **95 passed**。JavaScript 语法和 `git diff --check` 通过。上述浏览器测试有两个依赖弃用警告。已有 `KI-20261105-2142-001` 所述有序首页扫描成本仍存在。

## 2026-11-05 22:13：快速切换状态复核

执行者：Codex，环境说明其基于 GPT-6，完整模型 ID 未提供；版本仍为上述 `WORKTREE`。复核发现，在第 2 页输入表头筛选并在防抖结束前切走，返回后可能复用原查询的第 2 页。现在仅在缓存条件与当前条件一致时复用，否则重置游标并请求第一页。针对切换到 Structure 和时序子视图新增两项回归。

再次以指定原库、只读 Web 和有界面 Chromium 实测：在第 2 页输入 BTCUSDT 后立即切到 Structure，再返回 Data，显示第一页且 100 行均匹配筛选；按需计数和六小时分区跳转仍正常，分区结果为 18 行，浏览器控制台无错误。未通过 Web 修改原库。

最终合并回归 `python -m pytest tests/test_web_browser.py tests/test_web.py tests/test_plan062_web.py tests/test_plan072_fixes.py -q`：**133 passed**，有两个依赖弃用警告。原库按需加载和 `KI-20261105-2142-001` 的剩余限制不变。
