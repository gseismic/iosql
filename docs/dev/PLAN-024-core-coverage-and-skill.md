# PLAN-024-core-coverage-and-skill

## 背景

上一轮已经提交 `iosql.utils.mysqlite` 独立模块化加固。用户随后要求继续检查测试覆盖，确保测试能较完整覆盖 iosql 核心功能，如发现问题则修复；完成后在当前目录创建 `skills/` 并编写一个使用 iosql 的 skill。

## 覆盖审计结论

- `iosql.utils.mysqlite`：已有连接池、事务、动态建表、保留字、批量写入、冲突策略、类型推断和 SQL 工具测试。
- `iosql.query`：已有 CASE 表达式和 text where parser 测试。
- `iosql.partition`：已有 duration/calendar 分区、元数据恢复、分区裁剪、边界条件和大数据写入测试。
- `iosql.sqlite.Table` 时序契约：已有写入、查询、 explain、session、latest/asof、分段布局、upsert/ignore/append、repartition、write_buffer、truncate、日期分区等测试。
- 需要补强：根 `iosql.Database` 公开入口层、`Database.table(spec=...)` spec 兼容性、显式 `replace` 写入模式、分段表删除后的 segment 元数据刷新。

## 计划

1. 修复 `iosql.Database.close()` 关闭后仍缓存已关闭表对象的问题。
2. 补充核心契约测试：
   - `Database.table(spec=...)` 创建、复用、`table_names()` 和 close 后重新打开。
   - 已存在表对主键和列类型冲突的拒绝。
   - 分段表显式 `mode="replace"` 的主键覆盖语义。
   - 分段表 `delete()` 后刷新 segment `row_count/time_min/time_max`。
3. 创建 `skills/iosql-usage`：
   - `SKILL.md` 放核心工作流和触发描述。
   - `references/` 放更详细的使用模式和测试建议。
   - 生成并保留 `agents/openai.yaml`。
4. 运行核心测试、skill 校验和代码 review。
5. 提交计划、结果、代码和 skill 文件。

## 风险

- 入口层修复改变 `close()` 后复用同一个 `Database` 的行为；预期是更合理地重新打开表对象。
- 新增 skill 是仓库内资源，不保证自动被当前 Codex 实例发现，除非外部加载该 `skills/` 目录。
