# PLAN-058-mysqlite-scenario-examples

## 背景

用户要求在 examples 下新建独立文件夹，集中展示 `iosql.utils.mysqlite` 典型使用场景。
现有 `examples/mysqlite/` 只有 schemaless CRUD、批量写入、连接池三个基础示例，
缺少删列、重命名、备份、表/索引/meta 信息查询等运维向场景。

## 范围

新建 `examples/mysqlite-scenarios/`，每个脚本独立可运行（自动清理并重建自己的 runtime 目录）：

| 文件 | 场景 |
| --- | --- |
| `01_crud.py` | 建表契约、单条/批量写入、replace/ignore 冲突处理、update、delete、count、事务 |
| `02_add_drop_column.py` | ensure_column 动态加列、add_column 显式加列、原生 SQL DROP COLUMN（含索引限制演示）、SQLite 版本检查 |
| `03_query_variants.py` | select/select_one/select_all、query/query_all、fetchall/fetchone、execute_params、order_by/limit、count(where) |
| `04_rename_and_table_info.py` | ALTER TABLE RENAME（API 无 rename 方法，走原生 SQL 及其缓存注意点）、table_exists、get_all_tables、column_info/pk/default_values |
| `05_index_operations.py` | create_index、Table/Database 两级索引查询 API、drop_column_index、drop_all_index |
| `06_backup.py` | 通过 `conn.backup()` 在线备份到新文件、校验备份库行数一致 |
| `README.md` | 场景索引与运行方式 |

另更新根 `examples/README.md` 增补该目录入口。

## 关键事实（已核实）

- 本机 SQLite 3.46.0 ≥ 3.35，支持 `DROP COLUMN`；被索引列需先删索引
- mysqlite Table 无内置 rename/backup/drop_column 方法，均通过底层 SQL 或连接 API 完成——符合模块"SQL 完备工具"定位
- `_meta` 表由 `create_table(save_meta=True)` 维护，存 columns JSON/pk/comment 等

## 验证

逐个执行 6 个脚本，退出码 0 且输出符合脚本内打印的断言。

## 结果

见 PLAN-058-mysqlite-scenario-examples-OUTCOME.md
