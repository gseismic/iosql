# PLAN-084 迁移审查修复验证

- 时间：2026-10-18 15:55（Asia/Shanghai，最终汇总）。
- 模型：Codex（GPT-6，精确模型 ID 未提供）。
- 版本：WORKTREE，基准 `abc30fee0d812626d140bb3d9fcf7a870ac953e7`。
- 范围：本地协调与发布、Database/Table 缓存、SQLite/Parquet、存储身份识别、CLI、新增 file_lock 模块及两份回归测试。具体 Python 文件及最终校验和见 [source-hashes.txt](source-hashes.txt)。
- 环境：Linux，CPython 3.12.8，真实 SQLite/Parquet 文件，独立本机 PostgreSQL 14.22（端口 55478，账号 iosql）。未连接生产库。

## 目的与完成标准

验证两轮 review 合并的八项问题：原 URI 复用后不会被旧清理删除、合法表不漏拷、库级锁有无 fcntl 均有真实互斥路径、合法空目标可接收迁移、空表声明跨 generation 保留、内部重连后的句柄继续使用、秒级 calendar 正确剪枝、ui/web 都不占常驻共享租约。

## 复现与修复过程

首轮 32 项：29 失败、3 通过，见 [before.txt](before.txt)。该阶段有两处测试准备问题：Parquet 空表已持久化元数据，不能套用 SQLite 的纯 scaffold 断言；无 fcntl 测试不应先依赖尚未新增的平台辅助模块。这两处均已纠正。缺少 fcntl 导致普通 table_names() 失败由 [portable-before.txt](portable-before.txt) 单独确认。

扩展到 Parquet 时又发现未选中表复制后的读取锁文件缺失及空表 data 目录识别遗漏；一并修复。中间 [targeted.txt](targeted.txt) 记录 85 项通过，为新增最终超时用例前的阶段证据。

竞争复现通过清理钩子和另一线程固定顺序：首迁移发布并解锁→另一个调用者 rename→在原 URI 写入新库→首任务继续清理。测试检查新库行 777 保留、改名库包含旧目标与迁入行，操作均完成。无随机 sleep。

## 执行命令

新增组（最终 47 passed）：

```bash
uv run --no-sync --offline --with pytest --with psycopg2-binary --with duckdb \
  --with fastapi --with httpx --with uvicorn --with 'pip==25.2' \
  --with wheel --with setuptools \
  python -m pytest tests/test_plan084_migration_review.py tests/test_file_lock.py -q --tb=short
```

完整回归：

```bash
POSTGRES_DSN='postgresql://iosql@127.0.0.1:55478/postgres' \
uv run --no-sync --offline --with pytest --with psycopg2-binary --with duckdb \
  --with fastapi --with httpx --with uvicorn --with 'pip==25.2' \
  --with wheel --with setuptools python -m pytest tests -q --tb=short
```

针对性组在相同依赖与 PG 配置下运行：tests/test_plan084_migration_review.py、tests/test_file_lock.py、tests/test_safe_migration.py、tests/test_safe_migration_postgresql.py、tests/test_parquet_backend.py、tests/test_rename.py、tests/test_cli.py、tests/test_plan062_cli.py、tests/test_migration.py、tests/test_partition.py、tests/test_partition_string.py、tests/engines/sqlite/test_timeseries_contract.py、tests/engines/sqlite/test_partitioned_table_contract.py。

## 最终结果

| 验证 | 实际结果 | 记录 |
| --- | --- | --- |
| 新增用例 | 47 passed，3.97 秒 | [new-final.txt](new-final.txt) |
| 相关迁移、PG、维护、分区与契约 | 336 passed，20.93 秒 | [focused.txt](focused.txt) |
| 完整 tests | 927 passed、6 failed、15 skipped，134.06 秒 | [full-regression.txt](full-regression.txt) |
| 11 个 Python 文件 Ruff、源码校验和、空白检查 | 通过 | [static-check.txt](static-check.txt) |

日志入库时仅移除行末空白；断言、异常、统计及阶段信息保留。没有将阶段证据等同于最终全量通过。最终完整回归启动后 Python 源码没有修改，之后仅更新说明和验证产物。

## 原有失败与验证边界

以下六项与 PLAN-083 记录的基线一致（[此前独立基线对照](../20261018-12-WORKTREE/baseline-comparison.txt)），本次未修改对应实现或断言：

1. tests/test_plan056_fixes.py::test_mysqlite_integrity_error_rolls_back_connection
2. tests/test_plan062_mysqlite.py::test_group_rows_by_keys_groups_heterogeneous_rows
3. tests/test_plan062_mysqlite.py::test_auto_commit_false_requires_transaction_or_explicit_commit
4. tests/test_plan072_fixes.py::test_numeric_text_buckets_and_overflow
5. tests/test_plan072_fixes.py::test_web_candle_order_pagination_and_sort_validation
6. tests/test_web.py::TestReadOnlyApi::test_chart_line

前三项为既有 mysqlite 行为与旧断言差异，后三项为旧 Web 测试按单 JSON 解析流式输出。原有 15 个 skip 未计为通过。

没有 Windows 原机及断电验证。Win32 函数签名、共享/独占标志、句柄宽度、错误、冲突等待、FD 释放和目录同步分支通过替身验证；无 fcntl 的普通操作路径使用真实 Linux flock 替身。真实 Linux 共享/独占、跨线程/进程及 PG 测试通过。Windows 不宣称目录 fsync 的等价断电持久性。
