# ui-browse 过滤与默认展示优化 · 设计补充（2026-08-25）

> 上游文档：ui-20260823-overview.md（管理台总体设计）、
> ui-20260825-browse-功能定义.md（场景挖掘与优先级依据，product-feature-def-zh 方法论）。
> 本文是对 Browse Data 工作区的体验迭代设计，依据是对 DB Browser for SQLite
> （下称 DB4S）功能设计的研究与本库真实用户使用场景的差距分析。
>
> 场景编号对齐功能定义：S2=按关键值精确定位行、S3=确认最新数据、
> S4=范围/多值核查、S5=查看完整单元格、S6=验证回灌计数。

## 1. DB4S 研究结论（要点摘录）

### 1.1 默认展示
- **无分页**：滚动 + 懒加载（块大小 5 万行，后台线程 LIMIT/OFFSET 预取，
  未加载单元格渲染为 loading 态）；行数异步 COUNT，期间显示
  "determining row count…"，完成后 "X – Y of Z"。
- **默认无 ORDER BY**（自然顺序）；rowid 列默认隐藏。
- 列宽首次按内容自适应（有上限）；NULL 显示斜体浅灰字面量；长文本截断。

### 1.2 过滤（核心竞争力）
- **每列一个过滤输入框**，嵌在列标题与数据之间（FilterTableHeader）；
  另有全局框 "Filter in any column"。
- **前缀操作符语法**，不是 SQL WHERE：
  | 输入 | 语义 |
  |---|---|
  | `abc` | LIKE '%abc%'（默认包含匹配） |
  | `>5` `<=3.2` | 数值比较 |
  | `=abc` | 精确等于 |
  | `<>x` / `=NULL` | 不等 / IS NULL |
  | `1~5` | BETWEEN |
  | `/re/` | REGEXP |
- 多列条件之间 **AND**；全局框按空格拆词，词间 AND、任一列命中 OR。
- 200ms 防抖自动生效；过滤编译进 SELECT 由 SQLite 执行（服务端过滤，
  十亿行也不卡 UI）；过滤值按表持久化。
- 点列头排序（Ctrl+点多列），排序与过滤天然共存于同一条 SQL。

### 1.3 对我们的启示
DB4S 的过滤好用的本质：**入口贴着数据（表头行）、语法零学习（裸词即包含）、
即时反馈（防抖免点按钮）、服务端执行（大表不卡）**。

## 2. iosql UI 现状差距（场景 → 差距，编号见功能定义）

| # | 场景 | 现状 | 差距 |
|---|------|------|------|
| S2/S4 | 按值定位、范围核查 | 要到顶部构造器选 field/op/输入/点「应用筛选」 | 高频单列过滤路径太长，缺贴数据的快捷入口 |
| S3 | 确认最新数据 | 打开 Browse 默认 **asc**：首屏是最老的回灌数据 | 默认方向反直觉 |
| S1/S3/S4 | 读时间列判断新旧 | 毫秒时间戳裸奔 | 时间不可读，金融用户每天受害 |
| S1 | 连续扫读 | 页大小固定 200 不可调 | 效率低 |
| 异常路径 | 外部写入后核对 | 只有 R 快捷键，无可见刷新入口 | 可发现性差 |
| S4 | OR 组合条件 | 构造器支持 OR 序列化 | **后端 422，端到端坏损**（见 §4 Bug-1） |
| S5→S2 | 单元格值反哺筛选 | 详情面板只能看/复制 | 缺少「以此值过滤」闭环 |

## 3. 方案

### 3.1 P0-A 表头过滤行（对标 DB4S FilterTableHeader）
- thead 第二行，每列一个紧凑输入框；placeholder 按列类型给出语法提示。
- 语法（映射到现有 op 白名单 `== != > >= < <= between in`）：
  - 空 → 忽略；`>v`/`>=`/`<`/`<=` → 数值比较（coerce）
  - `=v` → 精确 ==（文本不 coerce，保住 `"007"` 类代码）
  - `!=v` → 排除；`a~b` → between；`v1,v2,…` → in
  - 裸词 → coerce 后 ==（金融 symbol 主场景为精确码；LIKE 核心 op 白名单
    冻结不含 like，placeholder 明示「精确匹配」，不假装支持模糊）
- 300ms 防抖自动应用；Enter 立即；Esc 清空本列。
- 表头行条件（AND 平铺）与高级筛选（ConditionBuilder，支持 or 分组）
  **合并为一个 where 数组下发**，二者 AND 叠加。
- 激活过滤的列头高亮；「清空过滤」一键清两处。

### 3.2 P0-B 时间戳可读化（对标 DB4S per-column display format）
- 判定：spec.time 列，或列名 `^(ts|time|timestamp)$` / `(_ts|_time|_at)$` 后缀，
  且契约类型为 integer。
- 渲染守卫：仅当整型值 ≥ 1e12（毫秒量级）才格式化为本地时间
  （复用 util.js `fmt.ts`），title/hover 保留原始毫秒值；详情面板双显。
- 工具栏开关记忆于 localStorage，切换只重渲染不重新请求。

### 3.3 P0-C 默认最新优先
- time 契约表的默认排序方向改为 **desc**（S1：打开即最新数据）；
  普通表维持 asc。keyset 游标对 desc 已验证可用（探针 §3）。

### 3.4 P1
- 页大小选择器 100/200/500/1000/2000（localStorage 记忆，改选重置首页）。
- 工具栏可见「刷新」按钮（保持当前页游标位置重取）。
- 详情面板新增「以此值过滤」：写入对应列表头过滤框（`=value`）并应用。

## 4. 附带 Bug 修复

### Bug-1 OR where 端到端坏损（P0，阻塞 S6）
- 现象：前端 ConditionBuilder 含 OR 时序列化 `[{"or":[…]}]`，
  `normalize_where` 要求每项含 field/op → 422。Browse/Query 两 Tab 同时受影响。
- 根因：UI HTTP 层未对齐核心查询能力。核心 `CaseExpression`（Case+Case）
  支持 or/and 树，但 JSON dict 形态从未被 UI 层解析。
- 修复（UI 层，不动核心冻结接口）：`normalize_where` 递归接受
  `{"or":[…]}` / `{"and":[…]}` 嵌套并校验叶子；`/rows` `/query` `/latest`
  `/delete` 将归一化结构转成 Case/CaseExpression 再下发。
  keyset 复合游标与 or 链相乘（`*`）已被 CaseExpression 运算符覆盖。

### 明确不做
- LIKE/contains：核心 op 白名单冻结（PLAN-054），涉及双引擎与分区裁剪
  语义，不在 UI 迭代中扩散；文本列裸词按精确匹配提供并在 placeholder 说明。
- IS NULL 过滤：Case 值类型校验拒绝 None（`col = NULL` 在 SQL 中恒假，
  需专门的 IS NULL 编译路径），跳过。
- 无限滚动懒加载：Web 场景下 keyset 分页内存可控且实现简单，保留分页模型，
  通过页大小选择器缓解。
