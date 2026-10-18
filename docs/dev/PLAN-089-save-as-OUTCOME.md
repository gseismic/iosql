# PLAN-089 结果：保留源库的 save_as 接口

- 日期：2026-10-18（Asia/Shanghai）。
- 计划：[PLAN-089-save-as.md](PLAN-089-save-as.md)。
- 设计：[save-as-20261018-api.md](../design/save-as-20261018-api.md)。
- 状态：完成，按用户要求随本次提交归档代码、示例、README、skill 及设计/计划/结果文档。

## 最终行为

- 新增 `Database.save_as()` 与 `BaseTable.save_as()`，三个 engine 的 Table 共享实现。
  前者默认复制全库，后者默认仅复制当前表；规范参数、默认值与结果类型沿用 `migrate_to()`。
- 复制后保留源业务数据、契约及源对象 URI，源和副本后续写入互不传播。
  选表、改名、目标布局覆盖、批次、预览、临时目录和锁等待均转交既有迁移入口。
- 协调层像处理 `migrate_to()` 一样排除 `save_as()` 的外层公共方法共享包装，
  独占锁仍由迁移执行器统一管理，避免同线程持共享锁时无法升级。
- `migrate_to()` 原本就保留源库；更新其 docstring 及使用说明，明确清理对象是
  工作区和目标退休版本。新入口不增加删除源库或自动清空目标的行为。

## 示例、README 与 skill

- 新增 `examples/18_save_as_compare_engines.py` 及 skill 中的对应副本：生成 20,000 行
  SQLite 数据，dry-run 后另存为 Parquet，并做行数验证。两份数据在示例结束后保留。
- 对两端执行完全相同的条件、投影、排序及完整结果消费，校验返回的 61 行一致，
  分别报告首次查询和预热后 5 次交替读取的中位耗时。另存、打开库和行数验证不计入
  查询耗时，首次查询不宣称为系统冷缓存。
- README 增加另存用法、目标已有数据的语义、性能比较方法及示例运行入口；
  主示例索引与 skill 索引同步到 01～18。
- skill 入口、API 参考、使用模式、设计契约及 UI 元数据加入 `save_as`。
  当前安装路径 `/home/lsl/.agents/skills/iosql-usage` 解析到仓库内对应目录，更新直接生效。

## 审阅与验证

环境为 Linux、Python 3.12、真实 SQLite/Parquet 目录库及本机独立 PostgreSQL 测试库。
自审了转发参数、继承后的公共方法包装、源对象绑定、既有目标合并和失败发布行为。

| 验证范围 | 结果 |
| --- | --- |
| 新增另存契约、PG 往返和两个示例入口 | **28 passed**，19 个无关用例未选中 |
| 扩展迁移、Parquet、分区、SQLite 契约与 mysqlite 回归 | **419 passed，3 failed，2 skipped** |
| 未修改 HEAD `d439c74` 的独立临时目录基线，同一组既有测试 | **391 passed，3 failed，2 skipped**，失败项目完全相同 |
| 20,000 行主示例手动执行 | 通过，源/目标各 20,000 行，查询结果 61 行一致 |
| 新测试及两个示例的完整 Ruff 检查 | 通过 |
| 修改的既有 Python 文件 Ruff 错误检查 `E9,F63,F7,F82` | 通过 |
| Markdown 本地链接与示例副本一致性 | 88 个链接、40 个示例副本通过 |
| skill 格式与 UI 元数据 | `Skill is valid!`，短说明 59 字符，默认提示和自动发现设置通过 |
| `git diff --check` | 通过 |

新增用例的最终命令：

```bash
# POSTGRES_DSN 指向独立的本机测试库。
uv run --no-project --with-editable '.[postgresql]' --with pytest --with ruff \
  python -m pytest -q tests/test_save_as.py tests/test_safe_migration_postgresql.py \
  tests/test_existing_database_examples.py -k save_as --tb=short
```

扩展组除上述文件外，还包含 `test_safe_migration.py`、`test_migration.py`、
`test_parquet_backend.py`、`test_partition.py`、`test_partition_string.py`、
`test_partition_integration.py`、SQLite 的 timeseries/partitioned-table/
partition-segment 契约、`tests/utils/mysqlite` 和 `test_plan062_mysqlite.py`。
没有把该针对性组表述为全仓库测试。

## 已确认的基线失败

三个失败在未修改 HEAD 的独立副本中重现，本轮未修改相应实现或断言：

1. `tests/utils/mysqlite/test_connection_pool.py::TestConnectionPool::test_process_budget_reclaims_idle_connections_across_pools`：
   组合运行时有效连接预算为 32，断言期望 3；单独运行通过，存在运行顺序依赖。
2. `tests/test_plan062_mysqlite.py::test_group_rows_by_keys_groups_heterogeneous_rows`：
   既有分组结果区分字典字段顺序，与旧断言不一致。
3. `tests/test_plan062_mysqlite.py::test_auto_commit_false_requires_transaction_or_explicit_commit`：
   显式事务内 `commit=False` 未按旧断言抛出异常。

以上失败均保留记录，未将扩展回归声称为全部通过；本轮新增用例全部通过。
