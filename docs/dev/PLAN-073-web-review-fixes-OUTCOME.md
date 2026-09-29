# PLAN-073 · Web Review 问题修复结果

## 结果

已完成 `docs/review/web-review-20260929-followup.md` 的 7 项修复，并按实施后的代码再次复审。没有修改与本任务无关的 `.zcodeignore`。

## 逐项结果

1. **清理预览范围固定：** 前端保存 dry-run 请求的 partition 快照；预览区显示该值。输入变化会禁用删除并显示“请重新预览”，打开确认框前和确认后都会再次比对；实际删除请求只发送保存的预览值。
2. **过滤计数有执行期限：** `/count` 带 where 时使用 5000ms 单次迭代期限。SQLite 用共享 deadline 与 SQLite progress handler 中断分段读取，并在有期限的路径跳过可能预扫描 legacy 数据文件的 segment 统计裁剪；PostgreSQL 按剩余期限设置 `statement_timeout`。超时作为 422 返回缩小筛选范围/添加时间条件的提示。无筛选计数仍走 manifest 求和。README 与使用指南已同步新指标；原预算测试断言已改为时间期限语义。
3. **`/spec` 切表竞态消除：** 请求发起时捕获表名；成功响应只写入对应表的缓存，只有当前页面仍显示该表时才渲染；旧请求失败也不会清空新表页面。
4. **多 symbol 曲线分组：** SQLite 与 PostgreSQL 分桶聚合增加可选分组列，先用全部所选 symbol 确定共用桶网格，再按 symbol 独立累计 sum/count。Web 返回每个 symbol 独立的 x/y 曲线，Charts 初次绘制和缩放重查均归一化多序列。为控制最多 4000 桶的响应规模，symbol 上限为 32；多 symbol candle 返回明确的 422。
5. **Explain limit 对齐 Data 页：** `/explain` 使用与 `/rows` 一样的 `max(1, min(body.limit, MAX_PAGE_LIMIT))` 处理，并把有效值写入 QueryPlan。
6. **JSONL 使用标准 JSON 序列化：** 每行先经过 `to_jsonable()`，`json.dumps` 设置 `allow_nan=False`。Decimal 转换后也检查有限性，非有限结果归一成 null；bytes 继续按统一序列化规则解码。
7. **文档同步：** 使用指南 Python 入口更新为 `iosql.web.create_app`，安装示例优先使用 `.[web]` 并注明 `.[ui]` 兼容；README 与指南均改为 Overview / Data / Structure / Charts / Maintain，并说明多 symbol 曲线能力。

## 检查

- `python -m py_compile`：修改过的 Python 实现文件与对应现有 Web 预算测试文件均通过语法编译。
- `node --check`：`app.js`、`tab-maintain.js`、`tab-charts.js` 均通过语法检查。
- `git diff --check`：通过。
- 未运行测试套件或浏览器验收。

## 已知边界

- 新增的 `iter_select_batches(timeout_ms=...)` 是可选期限参数；PostgreSQL 显式事务内传此参数会报 `BadArgument`，防止超时后留下被 PostgreSQL 标记为 aborted 的调用方事务。Web 计数路由不在显式事务内。
- 多 symbol 折线限制为最多 32 个标的；多 symbol candle 暂不支持并返回 422。
- JSONL 使用项目现有 `to_jsonable()` 约定：Decimal 转为有限 float，bytes 使用 UTF-8 替换解码。没有另行定义 Decimal 精度或任意二进制数据的 JSON 编码格式。
