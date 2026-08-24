# PLAN-059 · Browse Data 过滤与默认展示优化

> 设计依据：docs/design/ui-20260825-browse-filter-ux.md
> 场景与优先级依据：docs/design/ui-20260825-browse-功能定义.md
> （F02/F03/F04/F05=P0，F11/F12/F13=P1）

## 目标

以 DB Browser for SQLite 的 Browse Data 交互为参照，优化 iosql UI
Browse 工作区的过滤入口与默认展示行为；顺带修复 OR where 端到端坏损。

## 变更清单

### 后端（iosql/ui/app.py + tests/test_ui.py）
1. `normalize_where` 升级：递归接受 `{"or":[…]}` / `{"and":[…]}` 嵌套，
   叶子仍按 `{field,op,value}` 校验（含 between/in 值形态校验）；
   非法嵌套统一 422 带 hint。归一化输出保留嵌套结构。
2. 新增 `_build_case(where)`：把归一化 comps 转为 Case / CaseExpression。
3. `/rows`：user where 经 `_build_case` 后与 keyset 游标表达式相乘；
   `/query`、`/latest`、`/delete` 同步接入 or 能力。
4. 测试：
   - or 平铺两条 == 命中并集；and 嵌套；混合 [普通条件, {or:[…]}]
   - 非法嵌套（or 内叶子缺 field）→ 422
   - rows: where(or) + cursor 组合翻页不丢行
   - query/latest/delete 各一条 or 冒烟

### 前端（iosql/ui/static/js/ + style.css）
5. tab-browse.js 重构数据区表头：thead 第二行过滤输入框
   - `parseFilterInput(raw, colType)` → comp/null，语法见设计 §3.1
   - 300ms debounce 自动应用；Enter 立即；Esc 清空本列
   - 表头条件与 ConditionBuilder 条件合并下发（AND）
   - 激活过滤列头高亮 `.filtered`；「清空过滤」按钮清空两处
6. 默认排序方向：time 契约表 desc（`defaultSortCol` 返回 col+dir）
7. 时间戳可读化渲染 + 工具栏开关（localStorage 记忆），详情面板双显
8. 页大小选择器（100/200/500/1000/2000，localStorage 记忆）
9. 工具栏「刷新」按钮（保持当前页位置重取）
10. 详情面板支持动作按钮：browse 注入「以此值过滤」（`=value` 写回表头框）
11. style.css：filter-row 输入框、th.filtered 高亮、ts-cell 样式

### 文档
12. skills/iosql-usage/SKILL.md 可视化管理台段落补一句新交互。

## 验收标准
- 打开 time 契约表首屏为最新数据（desc）；点列头可切 asc。
- 在 symbol 列表头输入 `AAPL,MSFT` 回车 → in 过滤生效且计数同步；
  输入 `price` 列 `>100` 生效；`=007` 不被转成数字。
- 高级筛选 OR 条件在 Browse 与 Query 均正常返回（Bug-1 修复）。
- ts 列显示本地时间，hover 看到原始毫秒；开关关闭立即还原。
- 全部既有测试通过 + 新增后端测试通过。

## 不做（本轮明确排除）
LIKE 模糊、IS NULL 过滤、无限滚动懒加载——理由见设计文档 §4。
