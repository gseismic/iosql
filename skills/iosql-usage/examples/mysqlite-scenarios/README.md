# mysqlite 典型场景示例

`iosql.utils.mysqlite` 是低层单文件 SQLite 工具层（主 API 见 `examples/` 根目录）。
本目录按运维场景组织示例，顺序依日常读写和维护场景估计，并非访问统计。每个脚本可单独复制运行，每次在脚本所在目录的 `runtime/` 下创建独立数据库并保留：

| 文件 | 场景 |
| --- | --- |
| [01_crud.py](01_crud.py) | 库存扣减和预留记录在同一事务中提交或回滚 |
| [03_query_variants.py](03_query_variants.py) | 条件查询、聚合及外部值的参数化查询 |
| [05_index_operations.py](05_index_operations.py) | 创建和删除用户索引，保留主键约束索引 |
| [06_backup.py](06_backup.py) | 通过池化连接调用 sqlite3 backup API 备份并校验独立性 |
| [02_add_drop_column.py](02_add_drop_column.py) | 添加默认值列、原生 SQL 删列和刷新字段缓存 |
| [04_rename_and_table_info.py](04_rename_and_table_info.py) | 原生 SQL 重命名、表结构和 `_meta` 注意点 |

## 运行

在仓库根目录安装后，选择一个脚本运行：

```bash
python -m pip install -e .
python examples/mysqlite-scenarios/01_crud.py
python examples/mysqlite-scenarios/03_query_variants.py
python examples/mysqlite-scenarios/05_index_operations.py
python examples/mysqlite-scenarios/06_backup.py
python examples/mysqlite-scenarios/02_add_drop_column.py
python examples/mysqlite-scenarios/04_rename_and_table_info.py
```

## 与 examples/mysqlite/ 的分工

- `examples/mysqlite/`：入门三件套（schemaless CRUD、批量写入与冲突、连接池清理）
- 本目录：跨表事务、SQL 查询、索引和 schema 维护、备份等独立任务；基础 CRUD 与主键冲突见前述入门例子

## 安全提醒

mysqlite 的 `where` / `query_all()` / `execute*` 接受原生 SQL 字符串，
不要拼接未验证的外部输入；示例中的值均为字面量。
