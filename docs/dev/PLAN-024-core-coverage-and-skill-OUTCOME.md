# PLAN-024-core-coverage-and-skill-OUTCOME

## 完成内容

- 已提交上一轮 mysqlite 加固变更：`9c98f33 加固 mysqlite 独立使用契约`。
- 审计当前测试覆盖，确认核心覆盖面包括：
  - `iosql.utils.mysqlite` 独立模块、连接池、事务、动态表和 SQL 工具。
  - `iosql.query` 的 CASE/text where parser。
  - `iosql.partition` 的 duration/calendar、元数据恢复、裁剪和大数据场景。
  - `iosql.sqlite.Table` 的时序写入、查询、session、latest/asof、segment、repartition、buffer、truncate。
- 修复 `iosql.Database.close()`：关闭后清空表缓存，避免复用已关闭的底层表对象。
- 补充核心契约测试：
  - `Database.table(spec=...)`、`table_names()`、close 后重新取表。
  - 已有表的主键冲突和列类型冲突。
  - 分段表 `mode="replace"` 主键覆盖语义。
  - 分段表 `delete()` 后刷新 segment 行数和时间范围元数据。
- 新增仓库内 skill：`skills/iosql-usage`。
  - `SKILL.md`：iosql 使用决策和主流程。
  - `references/usage-patterns.md`：常用代码模式、写入模式、布局、mysqlite 和测试建议。
  - `agents/openai.yaml`：UI 元数据。

## Review 结果

- 发现并修复 `Database.close()` 缓存已关闭表对象的问题。
- 检查新增测试后合并同模块异常导入，避免无意义 import 噪音。
- 未发现需要进一步修改的核心行为问题。

## 验证

- `python -m pytest tests/sqlite/test_timeseries_contract.py -q`
  - 结果：`35 passed`
- `python -m pytest tests -q`
  - 结果：`162 passed, 14 skipped`
  - 说明：唯一警告为 pytest cache 尝试写入上级只读目录，不影响测试结果。
- `python /home/lsl/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/iosql-usage`
  - 结果：`Skill is valid!`
- `python -m compileall -q iosql tests skills`
  - 结果：通过
- `git diff --check`
  - 结果：通过

## 备注

- `iosql_mac` 仍为未跟踪路径，按用户旧分支引用保留，未纳入提交。
- `skills/iosql-usage` 位于当前仓库目录；是否被 Codex 自动发现取决于外部是否加载该 `skills/` 路径。
