# mysqlite 典型场景示例

`iosql.utils.mysqlite` 是低层单文件 SQLite 工具层（主 API 见 `examples/` 根目录）。
本目录按运维场景组织示例，每个脚本独立可运行、自动重建自己的数据库：

| 文件 | 场景 |
| --- | --- |
| `01_crud.py` | 建表契约、单条/批量写入、replace/ignore 冲突、update、delete、事务提交回滚 |
| `02_add_drop_column.py` | ensure_column 动态加列、显式加默认值列、原生 SQL 删列及"被索引列先删索引"限制 |
| `03_query_variants.py` | select/select_one/select_all、query/query_all 聚合、fetchall/fetchone、参数化执行、limit=0 |
| `04_rename_and_table_info.py` | 原生 SQL 重命名表及其 _meta 注意点、column_info/pk/default_values/table_exists/get_all_tables |
| `05_index_operations.py` | create_index、Table/Database 两级索引查询、drop_column_index/drop_all_index |
| `06_backup.py` | 通过池化连接调用 sqlite3 backup API 在线备份并校验 |

## 运行

```bash
python examples/mysqlite-scenarios/01_crud.py
python examples/mysqlite-scenarios/02_add_drop_column.py
python examples/mysqlite-scenarios/03_query_variants.py
python examples/mysqlite-scenarios/04_rename_and_table_info.py
python examples/mysqlite-scenarios/05_index_operations.py
python examples/mysqlite-scenarios/06_backup.py
```

## 与 examples/mysqlite/ 的分工

- `examples/mysqlite/`：入门三件套（schemaless CRUD、批量写入与冲突、连接池清理）
- 本目录：按场景速查的完整能力面，含删列、重命名、备份、元数据等运维操作

## 安全提醒

mysqlite 的 `where` / `query_all()` / `execute*` 接受原生 SQL 字符串，
不要拼接未验证的外部输入；示例中的值均为字面量。
