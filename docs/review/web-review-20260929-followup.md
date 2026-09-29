# iosql Web 专项 Review 报告（2026-09-29）

> 本文保留初审发现时的状态快照。7 项发现的修复与复查结论见
> `docs/review/web-review-20260929-final.md`；图表全量数据改造与实测记录见
> `docs/dev/PLAN-074-chart-raw-points-OUTCOME.md`。

## 审查范围

- 后端：`iosql/web/app.py`、`deps.py`、`errors.py`、`serializers.py`
- 前端：`iosql/web/static/` 下的 SPA、Data、Charts、Maintain 等模块
- 对照：Web 契约测试、README、`skills/iosql-usage/references/ui.md` 与相关设计文档
- 本文补充 `docs/review/code-review-20260929-full.md` 的 Web 审查，不重复列出其中已有问题。

本轮为静态代码与文档审查，没有运行测试或浏览器验收，也没有修改产品代码。以下影响来自代码路径分析，尚未通过运行时复现。

## 结论

本轮记录 **6 项 P2 行为问题和 1 项 P3 文档问题**。优先处理清理预览与实际执行范围脱节、计数预算没有限制实际扫描量这两项；其余问题影响切表正确性、图表含义、Explain 结果或 JSONL 导出兼容性。

## 发现

### P2-1 清理预览没有锁定实际清理范围

- **位置：** `iosql/web/static/js/tab-maintain.js:219-246,270-282`
- **问题：** `preview` 只保存 dry-run 返回值，没有保存当时使用的 partition。确认删除时，`cleanupBody(false)` 会重新读取 `partitionIn` 当前内容。
- **影响：** 用户预览某个分区后若修改分区输入，界面保留旧文件清单，但最终请求会清理新分区。表名确认不能确认预览时显示的实际范围。
- **建议：** 把 dry-run 的分区参数与结果一同保存，执行前校验参数未变；若已变则要求重新预览，并在确认区明确显示分区值。

### P2-2 `/count` 的扫描预算按命中行数计算

- **位置：** `iosql/web/app.py:88-89,736-753`
- **问题：** `MAX_COUNT_SCAN_ROWS` 注释和文档称其为扫描预算，但循环只把过滤后返回的 `len(batch)` 加到 `total`。高选择性的条件可能扫描整个大表却只命中少量行，因此不触发上限。
- **影响：** `/count` 仍可能对大表执行无界时长的流式读取；UI 给出的“超预算请缩小筛选范围”保护不能覆盖这类查询。
- **建议：** 让查询层提供实际访问/扫描行数预算，或将预算改为可执行的工作量限制；同时使常量注释、错误提示和文档准确描述被限制的指标。

### P2-3 快速切表时 `/spec` 响应可能污染当前表缓存

- **位置：** `iosql/web/static/app.js:380-393`
- **问题：** 请求 URL 读取了发起请求时的 `state.table`，但回调保存缓存时再次读取可变的 `state.table`。旧表的慢响应可能写入新表的缓存并渲染在新表页面。
- **影响：** 快速切表或网络延迟下，Data/Structure/Charts 可能短暂或持续使用另一张表的契约和列信息。
- **建议：** 发请求前将表名保存到局部变量；响应只写入该表对应的缓存，且仅在页面仍指向该表时渲染。失败回调也应检查请求表名，避免旧错误覆盖新页面。

### P2-4 多个 symbol 被聚合为一条均值曲线

- **位置：** `iosql/web/app.py:897-903,1070-1108`；设计预期见 `docs/design/ui-20260928-redesign.md:183-191`
- **问题：** 后端接受逗号分隔的多个 symbol，并以 `IN` 作为筛选；之后只按时间桶聚合 `col`，对所有选中 symbol 的值求平均，没有按 symbol 分组。前端最终只有一条 line 序列。
- **影响：** 输入多个标的时，返回的曲线代表同一桶内多个标的的平均值，而不是设计稿所述的多条独立曲线，容易造成错误解读。
- **建议：** 为每个 symbol 单独聚合并返回多序列；如果暂不支持多序列，则拒绝多个 symbol 或明确提示当前会合并计算。

### P2-5 Explain 请求忽略 `limit`

- **位置：** `iosql/web/app.py:728-734`；调用方在 `iosql/web/static/js/tab-data.js:557-560` 发送 `limit`
- **问题：** Explain 端点接收共用的 `RowsBody`，前端也发送当前页大小，但 `post_explain()` 调用 `explain_select()` 时没有传 `body.limit`。核心查询计划会记录 limit，并据此计算部分计划标记。
- **影响：** Data 页看到的 Explain 不是当前分页查询的完整计划，可能错误呈现限量查询与无上限查询之间的差异。
- **建议：** 将经过与 rows 相同边界处理的 limit 传入 `explain_select()`；若 Explain 有意忽略 limit，应移除请求字段并在界面说明。

### P2-6 JSONL 导出没有使用统一的 JSON 安全转换

- **位置：** `iosql/web/app.py:1194-1220`，尤其 `:1218`
- **问题：** JSONL 分支直接对原始行执行 `json.dumps(..., default=str)`。Python 默认会输出 `NaN`/`Infinity`，这不是标准 JSON；`Decimal`、bytes 等值也会被转换成字符串或 Python 对象表示。项目已有 `to_jsonable()` 处理有限浮点数、Decimal、bytes 等类型，但此处没有调用。
- **影响：** 含非有限浮点数的行无法被严格 JSON 解析器读取；其他特殊类型可能失去原有数值或内容语义。
- **建议：** JSONL 序列化前统一经过 `to_jsonable()`，并显式采用拒绝或归一化 NaN/Infinity 的策略；为 Decimal 和 bytes 约定稳定格式。

### P3-1 Web 使用指南仍指向旧 Python 模块与旧工作区结构

- **位置：** `skills/iosql-usage/references/ui.md:6-13,15-22`；`README.md:772-800`
- **问题：** 使用指南仍写 `iosql.ui.create_app`，仓库实际入口为 `iosql.web.create_app`；README/指南的工作区列表仍将 Browse Data 与 Query 分列，而当前界面已合并为 Data。安装示例使用兼容 extra `[ui]`，可以继续工作，但没有展示当前主名 `[web]`。
- **影响：** Python 用户照指南导入会失败；用户也会按过时的 Tab 结构寻找功能。
- **建议：** 更新导入路径为 `iosql.web`、安装示例为 `.[web]`（可另注明 `[ui]` 兼容），并把当前工作区列表同步为 Overview / Data / Structure / Charts / Maintain。

## 建议处理顺序

1. 修复 P2-1，确保删除范围与用户看到并确认的预览一致。
2. 明确并实现 P2-2 的工作量预算，避免筛选计数长时间占用服务线程。
3. 修复 P2-3、P2-5，保证页面契约和 Explain 计划对应当前表与查询。
4. 修复 P2-4、P2-6，确保图表与导出保留正确的数据语义。
5. 更新 P3-1 中的模块入口、安装名和 Tab 文档。
