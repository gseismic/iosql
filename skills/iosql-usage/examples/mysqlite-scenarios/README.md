# mysqlite 典型场景示例

`iosql.utils.mysqlite` 是低层单文件 SQLite 工具层（主 API 见 `examples/` 根目录）。
本目录按运维场景组织示例，顺序依日常读写和维护场景估计，并非访问统计。每个脚本可单独复制运行，每次在脚本所在目录的 `runtime/` 下创建独立数据库并保留：

| 文件 | 场景 |
| --- | --- |
| `01_crud.py` | 单条/批量写入、冲突处理、删除与事务提交回滚 |
| `03_query_variants.py` | 条件查询、聚合、参数化执行和 `limit=0` |
| `05_index_operations.py` | 索引的创建、查询与删除 |
| `06_backup.py` | 通过池化连接调用 sqlite3 backup API 在线备份并校验 |
| `02_add_drop_column.py` | 动态加列、显式加默认值列及原生 SQL 删列限制 |
| `04_rename_and_table_info.py` | 原生 SQL 重命名、表结构和 `_meta` 注意点 |

## 运行

```bash
python examples/mysqlite-scenarios/01_crud.py
python examples/mysqlite-scenarios/03_query_variants.py
python examples/mysqlite-scenarios/05_index_operations.py
python examples/mysqlite-scenarios/06_backup.py
python examples/mysqlite-scenarios/02_add_drop_column.py
python examples/mysqlite-scenarios/04_rename_and_table_info.py
```

## 与 examples/mysqlite/ 的分工

- `examples/mysqlite/`：入门三件套（schemaless CRUD、批量写入与冲突、连接池清理）
- 本目录：按场景速查的完整能力面，含删列、重命名、备份、元数据等运维操作

## 安全提醒

mysqlite 的 `where` / `query_all()` / `execute*` 接受原生 SQL 字符串，
不要拼接未验证的外部输入；示例中的值均为字面量。
