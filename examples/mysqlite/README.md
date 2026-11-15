# mysqlite 低层示例

本目录保留 `iosql.utils.mysqlite` 的少量可运行示例。它是 iosql 内部使用的低层 SQLite 工具层，适合需要直接操作单个 SQLite 文件、schema-on-write、批量写入或连接池控制的场景。

新项目优先使用仓库根目录的主时序 API 示例；只有明确需要低层 SQLite 工具时再使用本目录。下列顺序按读写、批量处理和连接管理的使用场景定性估计，并非使用统计；保留历史文件编号。

## 示例索引

| 文件 | 场景 |
| --- | --- |
| [01_schemaless_crud.py](01_schemaless_crud.py) | 自动建表、动态加列、基础 CRUD。 |
| [02_bulk_query_and_conflict.py](02_bulk_query_and_conflict.py) | 批量写入、聚合查询、复合主键、replace/ignore。 |
| [03_connection_pool_cleanup.py](03_connection_pool_cleanup.py) | 线程绑定连接、强制释放和重新打开数据库。 |

## 运行

在仓库根目录安装后，选择一个脚本运行：

```bash
python -m pip install -e .
python examples/mysqlite/01_schemaless_crud.py
python examples/mysqlite/02_bulk_query_and_conflict.py
python examples/mysqlite/03_connection_pool_cleanup.py
```

每次运行在脚本所在目录的 `runtime/` 下创建独立目录并保留结果；每个脚本可以单独复制运行。
