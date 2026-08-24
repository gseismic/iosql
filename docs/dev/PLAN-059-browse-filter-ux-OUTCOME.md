# PLAN-059 · 结果记录（OUTCOME）

> 计划：PLAN-059-browse-filter-ux.md
> 设计：docs/design/ui-20260825-browse-filter-ux.md
> 场景依据：docs/design/ui-20260825-browse-功能定义.md（product-feature-def-zh）

## 状态：COMPLETE（2026-08-25）

## 交付内容

### Bug 修复（P0）
- **OR/AND 嵌套 where 端到端坏损**：前端条件构造器的 `{"or":[…]}` 序列化
  此前被 `normalize_where` 以"缺 field"422 拒绝，Browse/Query 两 Tab 的
  OR 条件全部失效。修复：`normalize_where` 递归接受 or/and 嵌套分组，
  新增 `build_case()` 转为核心 `Case/CaseExpression`；`/rows` `/explain`
  `/count` `/query` `/latest` `/delete` `/export` 七个端点全部接入。
  keyset 复合游标与 or 链通过 CaseExpression `*` 运算组合（探针验证）。

### Browse Data 体验（P0）
- **F03 表头每列过滤行**：列标题下方内嵌输入框（对标 DB4S
  FilterTableHeader）。语法：裸词精确匹配、`>/>=/</<=` 数值比较、
  `=v` 强制文本（保住 `"007"` 类代码）、`!=v` 排除、`a~b` 范围、
  逗号分隔多值 in；300ms 防抖即输即查、Enter 立即、Esc 清本列；
  激活列头高亮；「清空过滤」一键清空表头行与高级构造器。
  表头条件（AND 平铺）与高级构造器（支持 or 分组）合并下发。
- **F02 默认最新优先**：time 契约表默认按时间列 **desc** 打开
  （金融用户首屏即最新数据），普通表维持 asc。
- **F04 时间戳可读化**：time 契约列 ∪ 整型且名字像时间的列
  （`ts`/`time`/`timestamp`/`datetime` 精确名或 `_ts/_time/_at` 后缀），
  毫秒量级值渲染为本地时间，悬停见原始值，详情面板双显；
  工具栏开关记忆 localStorage；关闭时显示原始数字（不做千分位）。

### Browse Data 体验（P1）
- **F11 页大小选择器**：100/200/500/1000/2000，localStorage 记忆。
- **F12 以此值过滤**：详情面板新增动作按钮，`=value` 回填对应列表头并生效。
- **F13 可见刷新按钮**：保持当前页游标位置与筛选重新拉取。
- **顺手修复**：详情面板标题承诺的 Esc 关闭此前从未实现，已补
  （输入框内 Esc 已 stopPropagation 不误关；确认弹窗打开时不穿透）。

## 关键实现决策

1. **数据更新只替换 tbody，不重建过滤输入框**——避免防抖触发重渲染时
   丢失输入焦点与未完成输入；排序/换列等非输入场景才重建骨架。
2. **LIKE/IS NULL 明确不做**（设计文档 §4）：核心 op 白名单冻结
   （PLAN-054），文本列裸词按精确匹配提供并在 placeholder 说明；
   `col = NULL` 恒假，需专门编译路径，跳过。
3. **时间列判定双来源**：契约（spec.time）权威 + 列名启发式兜底，
   值域守卫 ≥1e12 防误伤普通整型。

## 验证

- `pytest tests/`：**401 passed, 16 skipped**（含新增
  `TestNestedWhere` 10 项：or 并集、平铺+or 混合、and 嵌套、
  rows+cursor+or 不重不漏、count/query/latest/delete/export or 冒烟、
  5 类非法嵌套 422）。
- Playwright 真实浏览器冒烟（/tmp/opencode/ui-smoke/smoke.py，
  write 模式 + 750 行 ticks 演示库）全部通过：F02 默认 desc 与可读化、
  F03 裸词/比较过滤与高亮与清空、F04 开关、F11 页大小、F12 回填、
  排序箭头即时更新、Bug-1 OR 端到端（AAPL OR MSFT = 500 行）。
- 截图人工核对：过滤行、高亮、时间可读列、工具栏布局符合设计。

## Review 记录

自审修复 4 项：排序点击后列头箭头不更新（改为重建骨架）、翻页时
非法高级筛选静默丢条件（改为中止并提示）、tsToggle 闭包引用后置赋值
变量（改为函数声明提升）、Esc 关面板穿透确认弹窗（加 modal 守卫）。

## 遗留与后续

- LIKE 模糊搜索：待"精确匹配不够用"的真实证据再评估是否动核心
  op 白名单（功能定义 §7 待决策）。
- 场景占比为估算值（无埋点），后续可按使用反馈修正优先级。
