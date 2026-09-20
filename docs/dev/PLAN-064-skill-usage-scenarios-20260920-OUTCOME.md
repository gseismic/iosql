# PLAN-064 结果：完善 iosql Skill 使用场景与示例

## 结果

已完成 `iosql-usage` Skill 的场景化重构。Skill 现在可以根据用户面对的问题选择代表性示例，并且 Skill 目录中的示例代码与仓库 `examples/` 保持完整同步。

## 已完成变更

- 为 `skills/iosql-usage/SKILL.md` 增加标准 frontmatter、触发词和使用工作流。
- 按问题组织常用场景：建模、写入、普通查询、惰性批量读取、全局有序流式读取、布局维护、迁移、finance 和 mysqlite。
- 为主示例 `01` 至 `13`、finance 示例和 mysqlite 示例补充场景说明与选择建议。
- 新增 `examples/13_streaming_ordered_query.py`，覆盖 `query(..., order_by="open_time").iter_rows(...)` 的惰性有序读取场景，并将代码完整复制到 Skill 目录。
- 将仓库根目录的示例 README 和 finance README 完整复制到 Skill 的 `examples/` 目录。
- 更新 API、设计契约、使用模式和故障排查文档，明确 `order_by` 的流式路径使用游标与堆归并，不通过 `select()` 全量物化；同时说明有序读取的连接数和进程内存约束。
- 更新 Skill 的 agent 元数据，使其能够覆盖建模、流式读取、布局维护和迁移等常用请求。

## 验证结果

- 根目录与 Skill 目录的 28 个 Python 示例逐一校验，内容完全一致。
- 根目录 README、finance README 与 Skill 副本逐一校验，内容完全一致。
- 根目录及 Skill 副本共 56 个示例脚本全部运行通过。
- `python -m compileall -q iosql examples skills/iosql-usage/examples` 通过。
- `tests/test_existing_database_examples.py`：6 passed。
- 全量测试：686 passed、16 skipped、68 warnings。
- Skill frontmatter、相对链接、`git diff --check` 均通过。

## 代表性示例说明

- `13_streaming_ordered_query.py` 解决跨分区大结果集需要按时间排序且不能一次性加载到内存的问题。
- `01` 至 `12` 保留现有覆盖面，并为每个文件明确其所解决的一类问题；其中 `01` 和 `02` 有意分别展示底层 `Table` API 与 `Database.timeseries()` 便捷 API。
- finance 示例覆盖金融数据读取、批量写入和查询；mysqlite 示例覆盖连接池、事务以及并发/故障场景。

## 边界

- 示例运行验证使用本地 SQLite；需要外部 PostgreSQL 或其他外部服务的路径按文档约定跳过连接执行。
- 运行生成的数据库和缓存文件位于被忽略的 runtime 目录，不纳入示例代码同步。
