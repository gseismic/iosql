# PLAN-058-mysqlite-scenario-examples-OUTCOME

## 实施结果

`examples/mysqlite-scenarios/` 已创建，6 个脚本 + 目录 README 全部完成并逐个执行验证通过。

| 文件 | 场景 | 验证 |
| --- | --- | --- |
| `01_crud.py` | 建表契约、insert/insert_all、replace/ignore 冲突、update/delete/count、事务提交与回滚 | OK |
| `02_add_drop_column.py` | ensure_column 自动演进、显式加默认值列、原生 SQL 删列、"被索引列先删索引"限制演示（SQLite 3.46） | OK |
| `03_query_variants.py` | select/select_one/select_all、query/query_all 聚合、fetchall/fetchone 参数化行、execute_params、limit=0 | OK |
| `04_rename_and_table_info.py` | 原生 ALTER TABLE RENAME 及旧 Table 缓存注意点、column_info/pk/default_values/table_exists/get_all_tables、_meta 读取及其不同步警告 | OK |
| `05_index_operations.py` | create_index 幂等、Table/Database 两级索引查询、drop_column_index 按列反查、autoindex 约束坑演示 | OK |
| `06_backup.py` | 池化连接调用 sqlite3 backup API 在线备份、备份库一致性校验、备份独立性验证 | OK |

## 关键设计点

- **API 边界如实呈现**：mysqlite 无 rename/drop_column/backup 内置方法，示例按模块"SQL 完备工具"定位用原生 SQL / 连接 API 完成，并明确标注
- **真实坑位入档**：`Table.drop_all_index` 不过滤 autoindex（含主键表必报错）、原生 RENAME 不同步 `_meta`——示例不回避，直接演示失败路径与正确姿势
- 运行时数据落在 `examples/runtime/mysqlite-scenarios/<场景>/`，脚本自清理可重复执行

## 过程修正

- `select_one` 多位置参数误用 ×2（fields 需列表或逗号串）
- 示例 05 种子数据主键重复触发 IntegrityError（改 S0-S2 × ts 0-99）
- 移除一行误留死代码

## Review 追加修正

提交后自查发现示例 02 的删列验证输出自相矛盾：`DROP COLUMN` 实际成功，
但 `column_info()` 命中 `_table_info_cache`，而原生 `table.execute()` 不触发
`_invalidate_cache`，展示的是删列前的旧 schema。修正为确定性演示：

1. DDL 前显式调用 `column_info()` 填充缓存
2. 删列后打印"缓存视图"——仍含已删列，如实暴露 `execute` 不失效缓存的行为
3. 最终以 `fetchall("PRAGMA table_info(events)")` 直查真实 schema 作对照

该行为属 mysqlite 已知边界（原生 SQL 绕过 Table 缓存机制），示例选择如实
教学而非修改库行为；如需库级修复应作为独立计划处理。

## 关联文档

- 根 `examples/README.md` 低层 mysqlite 一节已补本目录入口
- 计划文件：PLAN-058-mysqlite-scenario-examples.md
