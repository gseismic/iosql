# PLAN-064: UI 重构实施（5-Tab 信息架构 + 时序体验修正）

- 日期：2026-09-28
- 设计依据：`docs/design/ui-20260928-redesign.md`（v0.2，含 SQLiteBrowser 逻辑研究 §10 与定案 D1–D12）
- 范围：**纯前端**（`iosql/ui/static/`）+ 测试断言文件路径同步；后端 `iosql/ui/app.py` 不改（API 面已满足全部需求）

## 实施项

| # | 项 | 设计依据 | 内容 |
| --- | --- | --- | --- |
| P1 | 6 Tab → 5 Tab | D1 | 合并 Browse Data + Query 为 **Data** 数据工作台（`tab-data.js` 新建，`tab-browse.js`/`tab-query.js` 删除）；分段控件 浏览\|时序\|截面（仅 has_time 表显示后两者）；Tab 顺序 Overview/Data/Structure/Charts/Maintain |
| P2 | Overview 驾驶舱 | D2 | 表卡片网格（行数/分区/segment/策略 + 健康信号），偏斜信号由 `/segments` 客户端按分区聚合行数（max/中位数 > 4 → ⚠ 偏斜 + 迷你条形图）；>30 表退化为紧凑表格；空库给建表引导 |
| P3 | 时间语义统一 | D3 | `IU.parseTimeInput`：接受 `YYYY-MM-DD[ HH:mm]` / 毫秒时间戳；Data 时序视图与截面 as_of、Charts start/end 全部走它 |
| P4 | Structure 分区偏斜可视化 | D4 | parts 条形图：key 可读化（13 位毫秒 → 日期）、偏斜热点 amber 高亮、点击分区行 → 跳 Data 并带时间范围过滤（hash query `f.<timeCol>=lo~hi`，兑现 Q2 可分享 URL） |
| P5 | 过滤收敛为一个真相 | D5 | where 预览条（人读文本 + 复制 JSON + 清空）；列头快筛（微语法，已有）∪ 高级构造器（默认折叠）合并进同一条 where |
| P6 | Charts 默认值与时间轴修正 | D7/Q3 | 数值列默认排除时间列；start/end 支持自然日期；`x.time` 时不再用 `String(Math.round(v))` 覆盖 X 轴（恢复 uPlot 原生时间轴） |
| P7 | Explain 附属 Dock | D9 | Explain 从独立查询页变为 Data 工具栏按钮 → 表格上方可折叠面板（复用 `IU.renderPlanPanel`） |
| P8 | 列选择器 | D12 | 「⚙ 列 x/y」下拉：搜索列名就地过滤 + 全部显示/恢复默认/仅键与时间列 + 类型标注；>20 列表默认只显示前 10 列 + 主键/时间列；最少保留 1 列 |
| P9 | Maintain 锁态增强 | — | 只读锁态给出 `iosql ui -s <库> --write` 可复制命令 |
| P10 | 状态栏吸收快捷键 | T8 | 顶栏快捷键提示移到底部状态栏（表上下文计数 + 1-5/←→/R） |

## 不做（本计划明确排除）

- 后端 API 任何修改（含 /parts 不补行数——由前端聚合 segments 获得）
- 真蜡烛图自绘（维持 OHLC 四线近似，Q3 定案）
- 亮色主题、多用户、远程部署

## 测试与验收

1. `node --check` 全部 JS。
2. `pytest tests/test_ui.py tests/test_plan062_ui.py`：静态断言文件路径 `tab-browse.js`/`tab-query.js` → `tab-data.js`（断言意图不变：badCols/sort-bad/排序列、s.pos--/pageSeq/spec.primary_key、IU.unwrap 系列、无 dry_run 预览）。
3. 真实库验收：`iosql ui -s /home/lsl/Z1/findata/crypto/binance/future_usdt/kline.factor.iosql`（只读），浏览器逐页验证 Overview 卡片/偏斜信号、Data 三视图、列选择器、Structure 条形图（31 段）、Charts 真实数据时间轴。
4. 演示库回归：`/tmp/iosql-ui-demo.iosql`（ticks/daily_bars/events 形态覆盖）。

## 结果文件

`docs/dev/PLAN-064-ui-redesign-OUTCOME.md`
