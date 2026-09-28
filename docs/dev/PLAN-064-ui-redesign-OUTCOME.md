# PLAN-064 实施结果：UI 重构（5-Tab 信息架构 + 时序体验修正）

- 日期：2026-09-28
- 计划：`docs/dev/PLAN-064-ui-redesign.md`
- 设计：`docs/design/ui-20260928-redesign.md`（v0.2）、高保真原型 `ui-20260928-redesign-prototype.html`
- 结论：**全部实施项完成，测试通过，真实库逐页验收通过**

## 逐项变更

| 项 | 结果 | 变更文件 |
| --- | --- | --- |
| P1 6→5 Tab 合并 | ✅ `tab-data.js` 新建（~950 行），`tab-browse.js`/`tab-query.js` 删除；分段控件 浏览\|时序\|截面（has_time 门控）；旧 hash `browse`/`query` 兼容映射到 `data` | index.html、app.js、tab-data.js |
| P2 Overview 驾驶舱 | ✅ 表卡片网格（行数/分区/segment/策略 + 时间范围 + 健康信号）；偏斜 = 最大分区/中位数 > 4（amber ⚠ + 迷你条形图）；>30 表退化为紧凑表格；空库给可复制建表示例；`data-part-signal`/`data-part-range` 渐进就地更新 | tab-overview.js、app.js |
| P3 时间语义统一 | ✅ `IU.parseTimeInput`（`YYYY-MM-DD[ HH:mm]` / 毫秒）+ `IU.timeField`（绿色 ✓ 解析回显）；时序 start/end、截面 as_of、Charts start/end 全部接入 | util.js、tab-data.js、tab-charts.js |
| P4 Structure 偏斜可视化 | ✅ 分区条形图（key 可读化 + 原始毫秒副标注、偏斜热点 amber、每分区行数/segs）；点击分区行 → `#/table/x/data?f.<timeCol>=lo~hi` 跳转 Data 且 URL 可分享；行数来自 `/segments` 客户端聚合（app.js `pumpParts`，含 segCount） | tab-structure.js、app.js |
| P5 过滤一个真相 | ✅ WHERE 预览条（人读文本，毫秒值附可读日期，AND/OR 琥珀高亮 + 复制 JSON + 清空）；列头快筛（微语法 auto-apply 300ms 已有）∪ 高级构造器（默认折叠）合入同一 where | tab-data.js |
| P6 Charts 修正 | ✅ 数值列默认排除时间列（真实库默认落 SMA(20)[close]）；`x.time` 时不再传 `space/values` 覆盖（根因：旧 uPlot 对 null 轴参数处理不佳），恢复原生可读时间轴；start/end 自然日期；422 扫描预算超限时错误盒透出服务端 hint | tab-charts.js |
| P7 Explain Dock | ✅ 工具栏按钮 → 表格上方可折叠面板（复用 `IU.renderPlanPanel`，`IU.unwrap(resp,'plan')`） | tab-data.js |
| P8 列选择器 | ✅ 「⚙ 列 x/y」下拉：搜索列名就地过滤 + 全部显示/恢复默认/仅键与时间列 + 类型标注；>20 列默认保留前 10 + 主键/时间列（真实库 24 列 → 10/24）；最少保留 1 列；**列显隐写入 URL `cols=`**（replaceState，可分享，优先级高于宽表默认） | tab-data.js |
| P9 Maintain 锁态 | ✅ 锁态给出 `iosql ui -s <库路径> --write` 可复制命令 | tab-maintain.js |
| P10 状态栏 | ✅ 顶栏快捷键提示移至底部状态栏；左段显示表上下文行数与全库合计 | index.html、app.js、style.css |

配套：style.css 新增 statusbar/lib-strip/table-card/mini-bars/seg-control/where-preview/dock/col-picker/time-field/cmd-hint 组件样式（tokens 全部沿用）；`tests/test_plan062_ui.py` 静态断言文件路径 `tab-browse.js`/`tab-query.js` → `tab-data.js`（断言意图不变）。

## 验证

1. **语法**：`node --check` 全部 8 个 JS 通过。
2. **测试**：`pytest tests/test_ui.py tests/test_plan062_ui.py` → **78 passed**；全量回归 `pytest tests/` → **686 passed, 16 skipped**（零失败，与基线一致）。
3. **真实库验收**（`/home/lsl/Z1/findata/crypto/binance/future_usdt/kline.factor.iosql`，只读，839MB / 418 万行 / 13 分区）：
   - Overview：两表卡片，kline_factor_1m 13 分区/4,183,200 行/范围 2021-12-29~2022-12-24/健康信号 + 条形图渐进填充
   - Data 浏览：24 列宽表默认 10/24；列选择器搜索"RSI"取消勾选 → 表格实时去掉 RSI(240) 列且计数 9/24；可读时间戳；keyset 翻页正常
   - Structure：13 分区条形图（可读 key + 行数 + segs），点击 2021-12-29 分区 → Data 页 WHERE `open_time between 1640736000000 ~ 1643328000000`，行数据落在分区内
   - Data 时序：`2022-01-01~2022-02-01 + BTCUSDT` 自然日期查询 → 绿色解析回显 + 1000 行返回
   - Data 截面：`BTCUSDT,ETHUSDT as_of 2022-02-01` → 恰好 2 行截面
   - Charts：BTCUSDT SMA(20) 2022-01-01~2022-03-01 → uPlot 原生时间轴（1/2…2/22 可读刻度）

## 已知限制 / 后续增强

1. `/chart` 服务端扫描预算 50 万行：全表无 symbol 绘图会 422（既有保护，前端已透出 hint）；可考虑后续把预算内自动降级采样作为增强。
2. Data 结果表（时序/截面查询结果）暂未做时间列可读化（浏览视图已做）；可复用 `tsCols` 逻辑补齐。
3. 列选择器列表 >500 列时未做虚拟滚动（当前真实库最大 24 列，不构成瓶颈）。
4. 原型文件 `ui-20260928-redesign-prototype.html` 为设计演示件（内置 mock 数据 + 状态切换器），与实现代码相互独立。
