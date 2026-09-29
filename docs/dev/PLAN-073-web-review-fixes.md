# PLAN-073 · 修复 Web 专项 Review 问题

## 目标

落实 `docs/review/web-review-20260929-followup.md` 记录的 6 项 P2 行为问题和 1 项 P3 文档问题，保持现有 Web 契约兼容，并让限制条件与实际行为一致。

## 实施范围

1. Maintain 清理预览保存其分区参数；实际删除使用预览快照，输入范围变化时锁住删除并要求重新预览。
2. 将 `/count` 的模糊“扫描行数预算”替换为真实查询执行时间预算。为 SQLite 和 PostgreSQL 批量迭代增加可选 `timeout_ms`，Web 过滤计数使用统一 5000ms 总期限；超时作为 422 可恢复错误返回。同步更新界面提示和文档。
3. `/spec` 请求捕获发起时的表名；成功与失败回调只缓存或更新仍对应当前请求表的页面。
4. 扩展两种引擎的分桶聚合，使其可以按 symbol 分组并共享全局桶网格；Web 返回独立曲线，Charts 在初次绘制和缩放重查时显示多条标的曲线。限制最多 32 个 symbol，保持响应规模有界；candle 多标的请求明确返回 422。
5. Explain 对 limit 使用与 `/rows` 一致的 `MAX_PAGE_LIMIT` 边界，并将有效 limit 传入核心查询计划。
6. JSONL 每行经 `to_jsonable()` 转换并使用 `allow_nan=False`，与 Web JSON 响应采用相同的有限数值、Decimal、bytes 表示。
7. 更新 Web 使用指南与 README：入口改为 `iosql.web`、优先安装 extra `[web]`、列出当前 Overview / Data / Structure / Charts / Maintain 工作区。

## 约束

- 只修改上述 Web、查询迭代/聚合所需的核心实现、指南和计划/结果文档。
- 保留工作区中与任务无关的 `.zcodeignore`。
- 按当前指令不新增或运行测试；通过静态代码复审与差异检查确认实施结果。
- 不提交 Git commit。

## 验收标准

- 清理请求的删除分区只能来自仍有效的预览快照；输入与快照不一致时删除不可执行。
- 带 where 的 `/count` 在全请求累计超过 5000ms 时中止，并返回准确的查询时间预算提示；非过滤计数不改变。
- 切表期间旧 `/spec` 响应不得写入新表缓存、渲染新表或覆盖新表错误状态。
- 多 symbol 图表响应含每个 symbol 独立的 y 序列，所有序列按同一桶网格对齐；单 symbol / 无 symbol 形状和语义兼容。
- Explain plan 的 limit 等于 `/rows` 使用的有效 limit。
- JSONL 输出严格符合标准 JSON，特殊值按统一序列化契约处理。
- README 与 iosql 使用指南准确描述当前导入路径、安装名和工作区。
- 完成后逐项复审改动并写 `PLAN-073-web-review-fixes-OUTCOME.md`。
