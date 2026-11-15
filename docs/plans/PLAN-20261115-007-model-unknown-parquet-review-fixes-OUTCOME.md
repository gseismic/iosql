# Parquet 六项 Review 问题循环修复结果

- 创建时间：2026-11-15 19:39 Asia/Shanghai。
- 实际执行模型：Codex（环境提供 GPT-6 系列信息，未提供可核实的实际模型 ID）。
- 分析与修复范围：`WORKTREE`，基准提交 `322485019a2cb0a4b461645a51f2be4c737fe616`。变更尚未提交，不将基准 HEAD 视为修复版本。
- 计划：[Parquet 六项 Review 问题循环修复](PLAN-20261115-007-model-unknown-parquet-review-fixes.md)。
- 相关工作区文件：`iosql/utils/parquet_files.py`、`iosql/utils/storage_identity.py`、`iosql/engines/parquet/table.py`、`iosql/engines/sqlite/table.py`、`iosql/_parquet_flat_publish.py`、`tests/test_parquet_review_regressions.py`、`tests/test_parquet_flat_migration.py`、`tests/utils/mysqlite/test_connection_pool.py`，以及本计划、结果和索引。原有未跟踪 `.playwright-cli/` 未处理。

## 完成情况

六条 Review 均已复核并修复，公开接口和 Parquet 清单格式保持一致。

| Review | 修复与代码依据 | 回归依据 |
| --- | --- | --- |
| P1 路径被通配扩展 | 在共享扫描入口逐字符转义 `[]*?`，引擎读取、修改前加载、查重、迁移计数和身份校验统一使用该入口，保留合法路径。`iosql/utils/parquet_files.py:52`；`iosql/engines/parquet/table.py:254`、`:296`、`:428`、`:602`。 | `tests/test_parquet_review_regressions.py:22`：并列库/表覆盖括号、星号、问号及组合，校验读取、追加、replace/delete、迁移及邻居数据。 |
| P1 完整集合校验 | 提取清单解析并按全部可见数据片计数，不重复计算已被清单排除的入口数据；缺片、损坏、重复、越界和符号链接均拒绝。`iosql/utils/parquet_files.py:14`；`iosql/utils/storage_identity.py:343`。 | `tests/test_parquet_review_regressions.py:52`、`:69`、`:107`：小批次迁移、完整集合损坏、替换后可见集合。 |
| P1 新目标父目录持久化 | 使用 `ensure_directory(root)`，并在提交决定前显式同步 `root.parent`；之前创建目录后同步失败的重试也重新同步。`iosql/_parquet_flat_publish.py:117`、`:141`。 | `tests/test_parquet_flat_migration.py:45`、`:76`：同步完成先于提交决定，失败不发布，重试需重新同步。 |
| P2 compact 保留 NaN | 改用 DuckDB 磁盘暂存和外部排序，按 1000 行返回批次；单连接预算先将结果写入临时中转，释放排序连接后再写目标。`iosql/engines/parquet/table.py:495`、`:517`。 | `tests/test_parquet_review_regressions.py:121`、`:211`：NaN、NULL、无穷、负零、文本和 blob 保留，覆盖连接预算 1/2/32 及多个排序批次。 |
| P2 Python NaN 顺序 | Python 排序与堆归并共用 Parquet 排序键，NaN 大于所有其他数值，多个 NaN 等价后继续比较后续排序列；NULL 顺序沿用契约。`iosql/engines/parquet/table.py:100`、`:485`、`:491`。 | `tests/test_parquet_review_regressions.py:151`：以原生 DuckDB 查询为独立参照，覆盖单文件、segment、分区、双向多列排序、top-N 和有序流。 |
| P2 ignore 空数据片 | 已有物理集合过滤后没有新增行时跳过数据片发布，仍执行必要的契约/列元数据更新；沿用 ignore 的 `rows_written=None` 契约。`iosql/engines/parquet/table.py:438`、`:447`。 | `tests/test_parquet_review_regressions.py:175`：无清单和已有清单均重复 ignore 十次，数据目录内容不变，新列读取为 NULL。 |

复审另修复 NaN 主键在 compact 中的重复判定：共享 chunk 分组增加内部键值钩子，Parquet 使用内部等价标记避免将重复 NaN 主键写入同一目标文件，业务行不改变；SQLite 保持原元组行为。代码见 `iosql/engines/parquet/table.py:513`、`iosql/engines/sqlite/table.py:1910`，回归见 `tests/test_parquet_review_regressions.py:195`。

## 计划偏差与验证中发现的问题

共享回归发现一项既有测试时机错误，已按计划补充事件处理：在未修改 HEAD 的独立 `git archive` 副本中，预置空闲上限 32 连接池后，原预算回收测试稳定得到 `32 != 3`。生产契约为活跃池取最小预算、全部空闲取最大配置值，原测试在连接归还后检查固定上限 3，因测试顺序而失败。

仅将该断言放到受限池活跃的上下文，并补充空闲大池参数；未修改连接池生产逻辑。见 `tests/utils/mysqlite/test_connection_pool.py:172`。原连接池文件单独为 10 passed，修正后为 11 passed；最终组合亦全部通过。本任务中已解决，无需新增长期问题条目。

## 实际验证

环境：macOS、Python 3.11.15、pytest 8.4.2、Arrow 1.3.0、Typer 0.12.5、Click 8.1.8、loguru；通过 `uv run --no-project --with ...` 提供依赖，不修改项目依赖声明。

1. DuckDB 2.0.0 最终组合回归：**657 passed、16 skipped、126 warnings**，321.15 秒。
2. DuckDB 1.1.0 最低支持版本核心回归：**115 passed、1 skipped**，40.51 秒。文件集合为下述命令前四个测试文件。
3. compact 及失败恢复专项：16 passed（包含于扩大回归，不累计为额外独立覆盖）；连接池专项 11 passed。
4. `ruff check` 本次八个 Python 文件通过；`git diff --check` 通过。

最终组合测试命令（DuckDB 2.0.0）：

```bash
uv run --no-project \
  --with 'duckdb==2.0.0' --with 'pytest==8.4.2' --with 'arrow==1.3.0' \
  --with loguru --with 'typer==0.12.5' --with 'click==8.1.8' \
  python -m pytest -q \
  tests/test_parquet_review_regressions.py tests/test_parquet_backend.py \
  tests/test_migration_performance.py tests/test_parquet_flat_migration.py \
  tests/engines/sqlite tests/utils/mysqlite \
  tests/test_partition.py tests/test_partition_string.py \
  tests/test_partition_integration.py tests/test_partition_integrity.py \
  tests/test_safe_migration.py tests/test_plan084_migration_review.py \
  tests/test_plan093_review.py tests/test_migration.py \
  --disable-warnings --tb=short -rs -p no:cacheprovider
```

16 个跳过项为 11 个已废弃旧接口用例、5 个需要独立可写文件系统的用例；最低版本核心的 1 个跳过同样为跨文件系统用例。既有较新 Typer/Click 的测试兼容性待办 `TD-20261115-1621-001` 保持原范围，本次使用上述已有兼容组合。

## 最终复审与边界

最后一轮依据修改后的实际源码复核六条问题和关联修改，重新检查全部扫描调用、清单异常、零新增元数据、NaN 多列比较和重复主键、单连接资源释放、compact 构建失败隔离、提交前同步及恢复。该轮没有再修改代码，必要验证均通过；本次审查范围内未发现待修复的逻辑问题。

未执行真实断电、Windows 原机及真实独立跨盘验证；目录同步验证为 POSIX 调用顺序和故障注入，不将其描述为实际断电试验。未执行 commit/push，修复版本仍为 `WORKTREE`；无本次范围内遗留事项。
