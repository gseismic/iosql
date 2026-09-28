# PLAN-071 结果：Charts 可视区按需加载（缩放只查窗口）

日期：2026-09-28
计划：PLAN-071-chart-viewport-zoom.md

## 结论

Charts Tab 现在支持"只绘制可视区"：进入页面时全范围降采样出图（≤2000 点，
一次请求），拖拽缩放后**仅按可视区 start/end 重新查询**（2000 桶重新分布在
局部窗口，放大即局部高分辨率），双击复位回到全范围。浏览器实测（418 万行
大表）全链路无回环、无请求风暴。

回答用户的问题："当前是这样吗"——此前不是：PLAN-070 之后传输/渲染已是
降采样点（服务端 SQL 聚合、内存有界），但每次绘制仍扫描窗口内全部数据，
且缩放不会按可视区重新查询；本次改造后，缩放/复位的读取范围与可视区一致。

## 变更清单（逐条）

1. `iosql/web/static/js/tab-charts.js`（唯一产品代码改动，服务端零改动）
   - `setScale` hook：x 刻度变化（拖拽缩放）→ 250ms 防抖 → 视口重查；
   - 首个 setScale（构造期，数据范围）只初始化 `viewApplied`/`fullScale`
     基准、不触发查询——构造时 `chart.scales.x.min/max` 尚未提交（null），
     不能事后读取（实测教训）；
   - 去重容差取约 2.5 个桶宽：`setData(resetScales=true)` 后 uPlot 异步重定
     刻度使数据范围相对请求窗口内缩约一个桶宽，容差小于桶宽会形成
     "重查→内缩→再重查"回环（实测曾连续 4+ 次请求）；容差按
     `span × 2.5 / buckets` 随桶数自适应；
   - 请求序号 `viewSeq` 守卫丢弃过期响应；`suppressScale` 标志抑制
     `setData` 内部 setScale 的同步回环；
   - `fetchSeries(base, col, winMs)` 增加可视区毫秒窗口参数；时间轴按
     视口秒值 ×1000 换算，非时间轴直接用原始 x 值；
   - 视口重查复用当前 `active` 序列配置（line 单列 / candle 四列），
     经 `buildUData` 重建后 `chart.setData(data, true)` 原地更新，不销毁
     重建实例；
   - 双击复位：uPlot v1.6 无内置缩放复位（实测 dblclick 只重置光标），
     前端在画布上监听 dblclick → `chart.setScale('x', 初始范围)` →
     hook 判定 `closeWindow(win, fullScale)` → 以无 start/end 参数全量重查；
   - `viewNote` 状态行：加载中（spinner）/「全范围|可视区 + 窗口 + 点数」，
     全范围时附"拖拽缩放 · 双击复位"操作提示；
   - `resetViewState()`：重新绘制（点击"绘制"/切换配置）时取消防抖、
     作废在途请求、清空视口状态。

2. `tests/test_chart_pushdown.py`
   - 新增 `TestFrontendViewportLoading` 6 例静态契约：hook 接线、防抖/去重/
     序号守卫、setData 抑制回环、双击复位全量重查、视口参数、加载态与
     作废逻辑；
   - 顺带把 `MAX_CHART_SCAN_ROWS` 测试的局部 import 提升为模块级
     `web_app` 引用。

## 验证（418 万行 kline_factor_1m，临时服务 8693 + 浏览器实测）

| 操作 | 网络请求 | 结果 |
|------|---------|------|
| 进入 Charts | 1 次，无 start/end | 全范围 2000 点，note「全范围 …」 |
| 拖拽缩放（全宽→约 1/4 窗口） | 1 次，start/end = 视口窗口 | 2000 点重分布到 4 个月，局部分辨率 ×4 |
| 双击复位 | 1 次，无 start/end | 回到全范围 2000 点 |
| 反复缩放 | 无请求风暴 | 防抖 + 桶宽容差去重生效 |

回归：全量测试 723 passed / 16 skipped（新增 6 例前端静态契约全绿）。

## 行为说明与取舍

- 缩放重查的 buckets 沿用配置值（默认 2000）：放大时同样的点数画在更小
  窗口里，分辨率自然提升；这正是"可视区高分辨率"的实现方式；
- 双击复位必须前端自实现（uPlot v1.6 的 dblclick 只重置光标状态）；
- 未做滚轮缩放/平移（uPlot 核心无此交互，需引入插件）与可视区结果缓存
  （重复回到同一窗口会重查，暂接受）；
- 服务端零改动：start/end 触发既有分区裁剪，可视区越小扫描越少。
