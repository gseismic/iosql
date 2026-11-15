# 示例规范检查与修正：结果

创建时间：2026-11-15 17:52（Asia/Shanghai）\
执行模型：Codex（环境说明基于 GPT-6，未提供可核实的实际模型 ID）\
结果范围：WORKTREE；基准提交 `399473efa7abb5d9cfc59941bf06dfa1ab3f31ae`。相关未提交文件为 `README.md`、`examples/`、`skills/iosql-usage/examples/`、`iosql/engines/parquet/table.py`、`iosql/utils/storage_identity.py`、`tests/test_example_scenarios.py`、`tests/test_migration_performance.py`、本计划及结果和计划索引；结果尚未固定为提交。任务外的 `.playwright-cli/` 保留。

关联计划：`docs/plans/PLAN-20261115-006-model-unknown-example-standards.md`。

## 完成情况

按用户指定的 `library-examples-zh` 逐个核对全部 36 个 Python 示例、PostgreSQL 启动脚本及四份示例索引。检查覆盖独立入口、真实任务、重复场景、公开接口、声明依赖、前置检查、资源释放、预期结果和数据去向；保留原有路径和编号，通过索引安排阅读顺序。

1. 普通表入门改为按类别、库存筛选商品并按价格排序，与时序行情入门区分；实现见 `examples/01_quickstart_table.py:23`、`examples/01_quickstart_table.py:36`。缓冲写入示例移除无关的 segment 配置，继续保留独立的两级裁剪示例。
2. 已有库示例明确 SQLite/Parquet 契约前提；重分区在准备演示源数据前检查外部源和目标，源、目标行数使用计数接口，避免为显示行数全量读取已有库，见 `examples/10_existing_database_repartition.py:29`、`examples/10_existing_database_repartition.py:78`、`examples/10_existing_database_repartition.py:88`。移除与增量写入、重分区任务无关的默认连接预算展示。
3. 低层 CRUD 重复展示收敛为库存扣减与预留记录的事务：失败回滚、成功后同时提交，见 `examples/mysqlite-scenarios/01_crud.py:30`、`examples/mysqlite-scenarios/01_crud.py:40`。删列后通过公开的 `invalidate_cache()` 刷新字段缓存，见 `examples/mysqlite-scenarios/02_add_drop_column.py:39`；索引例准确展示删除用户索引、保留主键约束索引，见 `examples/mysqlite-scenarios/05_index_operations.py:31`。精简查询、重命名和备份例，统一低层示例的上下文资源释放，删除备份“不会阻塞写入”的过强说明。
4. PostgreSQL 示例补充目标 schema 的 CREATE 检查；两个固定表入门例以可管理自己的示例表为前提并提前检查所有者角色，见 `examples/01_quickstart_postgresql.py:31`。迁移例另检查数据库 CREATE，说明暂存和控制 schema 的权限条件，见 `examples/11_sqlite_to_postgresql_migration.py:47`。没有自动向业务角色授权或降低分区布局锁。查询例清理连接显式关闭，见 `examples/15_postgresql_timeseries_query.py:80`。
5. 启动脚本增加专门的前置检查，自动发现 Linux 与 Homebrew 版本目录，见 `examples/start_postgresql_test.sh:14`、`examples/start_postgresql_test.sh:25`。修正环境配置读取说明。
6. 四份示例索引补全独立运行的安装说明、直接文件链接、定性频次排序及场景分工；金融索引不再要求设置 `PYTHONPATH`。同步所有示例镜像，共 42 个文件逐项一致；仓库 skill 的现有入口路径保持有效。
7. 实际运行确认 DuckDB 2.0 移除了关系对象的 `fetchone()`，阻断原有 Parquet 迁移/另存例。[官方 Relational API](https://duckdb.org/docs/api/python/relational_api)提供关系结果的替代读取接口。本次将三处聚合计数改为 `fetchall()[0][0]`，只读取一个计数行，见 `iosql/engines/parquet/table.py:558`、`iosql/utils/storage_identity.py:342`、`iosql/utils/storage_identity.py:345`。保留锁、异常处理和公开接口，并通过 1.5.4、2.0.0 双版本验证。

## 验证结果

本机环境：macOS、Python 3.11.15、PostgreSQL 14.19（Homebrew）。数据库验证使用本轮新建的临时 SCRAM 密码认证实例，不使用业务数据库。

| 验证 | 结果及范围 |
| --- | --- |
| DuckDB 2.0.0 扩大回归 | **593 passed、4 skipped、0 failed**；覆盖示例、真实密码与密码文件、权限不足提前退出、Parquet、迁移、引擎识别、SQLite 时序与分区契约、低层 mysqlite；含原有 10,000 分区 upsert 压测。 |
| DuckDB 1.5.4 兼容回归 | **55 passed**；`tests/test_parquet_backend.py` 和 `tests/test_migration_performance.py`，包含空文件、行数不符、复用连接计数及损坏文件检测。 |
| 安装包单文件验证，DuckDB 2.0.0 | **36/36 通过**；Typer 0.12.5、Click 8.1.8。每例复制到独立目录，清除 `PYTHONPATH`/`PYTHONHOME`，通过非 editable 安装包运行。两个改动核心文件与工作区 SHA-256 一致。 |
| 当前默认依赖安装的单文件验证 | **36/36 通过**；实际解析为 Arrow 1.3.0、DuckDB 1.5.4、Typer 0.19.2、Click 8.3.0、psycopg2 2.9.10。使用同样的独立目录和正常安装方式。 |
| 既有示例专项 | **8 passed**；`tests/test_existing_database_examples.py` 从仅包含例子和测试的临时副本运行，覆盖主示例与镜像，使用 DuckDB 2.0.0。 |
| 已有外部源和跨后端目标 | SQLite、Parquet 两种外部源各运行增量写入及重分区，**4 次通过**；交叉写入另一种后端后逐行核对源、目标相等，均为 2 行。 |
| PostgreSQL 启动脚本 | 单文件副本验证程序缺失提示、缺失时不建 runtime、自动发现 PostgreSQL 14、首次启动及重复启动；均通过，启动实例已停止。 |
| 静态与文档检查 | `ruff check`、`bash -n`、36 个文件头和语法、42 个镜像文件比较、README/示例索引链接、`git diff --check` 通过。 |

4 个跳过项是当前机器没有独立可写内存文件系统或独立可写文件系统的迁移发布测试：`tests/test_safe_migration.py:312`（2 项）、`tests/test_safe_migration.py:436`、`tests/test_parquet_flat_migration.py:130`。未将这些条件报告为通过。

扩大回归使用以下文件集合（使用 pytest 8.4.2、DuckDB 2.0.0、Typer 0.12.5、Click 8.1.8，项目安装 PostgreSQL/web extras，设置独立测试库的 `POSTGRES_DSN`、`IOSQL_EXAMPLE_TEST_POSTGRES_DSN` 和认证测试所需的 `IOSQL_AUTH_TEST_POSTGRES_*`）：

```text
tests/test_example_scenarios.py
tests/test_postgresql_credentials.py
tests/test_parquet_backend.py
tests/test_migration_performance.py
tests/test_migration.py
tests/test_safe_migration.py
tests/test_parquet_flat_migration.py
tests/test_local_uri_engine.py
tests/engines/sqlite/test_partitioned_table_contract.py
tests/engines/sqlite/test_timeseries_contract.py
tests/engines/sqlite/test_partition_segment_combinations.py
tests/test_partition.py
tests/test_partition_integration.py
tests/test_partition_string.py
tests/utils/mysqlite
tests/test_plan062_mysqlite.py
```

## 偏差、自查及剩余边界

- 按计划复核后增加了直接阻断示例运行的 DuckDB 计数兼容修复，未将旧依赖锁定作为修复；新增独立文件计数回归并复用既有深度验证和迁移测试。首轮完整专项为 126 passed、7 failed，其中四项来自该兼容问题，另三项是新增权限测试将 libpq options 空格编码成 `+`；已改为 `%20`，扩大回归中全部通过。
- 既有示例专项首次并行执行时，两个性能例在原有 20 秒时限超时；待扩大回归结束后，保持原测试和时限单独重跑，8 项全部通过。没有修改测试时限或缩减 20,000 行性能演示。万分区压测在尝试停止之前已经结束，最终扩大回归完整通过，没有测试被实际中止。
- 自查核对场景正交性、前置检查顺序、异常清理、主键约束保留和新旧 DuckDB 读取方式；本轮示例检查无未完成项。权限检查不替代运行时授权校验；既有控制 schema 的对象权限由实际迁移验证。
- 本次实跑平台为 macOS，未作 Windows/Linux 原机验证；四项文件系统条件如上。此前 CLI 测试兼容性待评估事项仍按 `TD-20261115-1621-001` 跟踪，本轮默认依赖下的示例通过不代表该事项全部解决。
- `GOAL.md` 未创建或修改；没有 commit/push。临时服务仅用于本轮验证，未向业务数据库写入或修改角色授权。
