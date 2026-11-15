# SQLite 与 mysqlite 十一项 Review 循环修复结果

- 创建时间：2026-11-15 21:28（Asia/Shanghai）。
- 执行模型：Codex（环境说明基于 GPT-6，未提供可核实的实际模型 ID）。
- 修复版本：`WORKTREE`；基准提交 `f4cd8431e7e14bd6ce8ea5b2a25a8156d2ae445d`。开始时工作区干净，结果尚未固定为提交。
- 范围：用户提供的十一项 SQLite/mysqlite Review 及必要的相邻路径；计划为 `docs/plans/PLAN-20261115-008-model-unknown-sqlite-review-fixes.md`。
- 本轮文件：`iosql/engines/sqlite/table.py`、`iosql/engines/parquet/table.py`、`iosql/utils/mysqlite/sql.py`、`iosql/utils/mysqlite/table.py`、`tests/test_sqlite_review_regressions.py`、`tests/test_plan093_resources.py`、本计划及结果和计划索引。

## 完成情况

十一项均已修复，公共方法签名保持不变。

| Review 项 | 修复与直接证据 |
| --- | --- |
| 补列重试丢失此前批次 | 在写当前批次任何行前补齐结构，不再失败后重放部分成功的语句组；同一事务包含补列及全部批次，自有事务失败整体回滚，显式事务由外层控制。`iosql/utils/mysqlite/table.py:527`；回归 `tests/test_sqlite_review_regressions.py:19`、`:32`。 |
| NumPy 标量保存为 BLOB | 公共位置/命名绑定转换 integer、float、bool、text、bytes 标量；覆盖单行、批量、更新、查询、分段查重和 longdouble。`iosql/utils/mysqlite/sql.py:31`、`iosql/utils/mysqlite/table.py:332`；回归 `tests/test_sqlite_review_regressions.py:45`、`:70`、`:89`。 |
| 手动建表缺逻辑登记 | 成功建表同步登记 schema 和主分区；分区/segment 先持久化逻辑契约，首次写入再创建正确物理文件；保留幂等建表和重开后的契约。`iosql/engines/sqlite/table.py:5802`；回归 `tests/test_sqlite_review_regressions.py:98`、`:248`、`:269`。 |
| 全局排序丢失 collation | 排序与堆归并共用物理列的 NOCASE/RTRIM/BINARY 键，保留 ASCII、NUL、空格语义。`iosql/engines/sqlite/table.py:191`、`:4664`；回归 `tests/test_sqlite_review_regressions.py:121`。 |
| 混合存储类排序错误 | select 与有序流共用 `_SqliteSortKey`，按 NULL、数值、文本、BLOB 的 SQLite 顺序比较。`iosql/engines/sqlite/table.py:4065`；同一回归覆盖 ASC/DESC、混合值及 LIMIT。 |
| latest symbol 精确比较误过滤 | SQL 保留匹配结果，跨文件分组及请求排序使用物理亲和性/排序规则；REAL 请求比较使用 NUMERIC 亲和性，避免存储舍入错误合并请求。`iosql/engines/sqlite/table.py:2351`、`:5198`；回归 `tests/test_sqlite_review_regressions.py:143`、`:156`、`:279`。 |
| compact 等价主键落同一批 | 预取物理比较契约，按 collation 等价键切分 chunk，保留合法跨段重复；临时外部排序保留 collation。Parquet 的私有 override 同步接收预取契约，保留其 NaN 语义。`iosql/engines/sqlite/table.py:1928`、`iosql/engines/parquet/table.py:513`；回归 `tests/test_sqlite_review_regressions.py:170`、`:279`。 |
| 样本内后续不兼容类型漏检 | 收集样本中所有列/类型组合，拒绝不兼容组合，保持既有数值互通规则。`iosql/engines/sqlite/table.py:1216`；回归 `tests/test_sqlite_review_regressions.py:185`。 |
| REAL 分区精度转换误路由 | 按 SQLite 实际存储转换后的分区列路由，不修改调用者行对象；普通文件和 row segment 的实际值等值查询及 explain 均可定位。`iosql/engines/sqlite/table.py:5031`；回归 `tests/test_sqlite_review_regressions.py:194`。 |
| 列统计缓存持续增长 | 全局有界 LRU（256），同文件只保留一个版本，关闭表清理所属缓存；查询期间版本变化不缓存旧快照，返回值与缓存隔离，fork 后重建锁并清空缓存。`iosql/engines/sqlite/table.py:2849`、`:5850`；回归 `tests/test_sqlite_review_regressions.py:210`、`:222`、`:293`、`:326`。 |
| save_meta=False 跳过缓存失效 | 两条实际建表路径都在 DDL 成功后立即失效结构缓存，不依赖附加元数据保存。`iosql/utils/mysqlite/table.py:195`、`:576`；回归 `tests/test_sqlite_review_regressions.py:234`。 |

## 验证结果

环境：macOS、Python 3.11.15、SQLite 3.53.0、NumPy 2.4.6、DuckDB 2.0.0、pytest 8.4.2、Arrow 1.3.0、loguru 0.8.0、Typer 0.12.5、Click 8.1.8、FastAPI 0.121.0、httpx 0.28.1、uvicorn 0.54.0、psycopg2-binary 2.9.10；通过 uv 提供依赖，未修改项目依赖声明。

| 检查 | 结果 |
| --- | --- |
| 扩大本地回归（下述命令） | **1535 passed、126 skipped、127 warnings**，606.48 秒；没有失败。 |
| 最终代码的新增专项及 compact/fork 组合 | **63 passed、61 deselected**，11.88 秒；包含全部 56 项新增专项。 |
| DuckDB 1.1.0 最低支持版本，Parquet 核心及前轮 Review 回归 | **73 passed**，60.34 秒；本轮 Parquet 私有 override 兼容验证通过。 |
| latest SQLite 对照实验 | 6 种列声明、72 组请求顺序全部一致，覆盖 integer、REAL、TEXT、NOCASE、RTRIM、ANY、大小整数、数值文本和 BLOB。 |
| 静态及差异检查 | 六个改动 Python 文件的 ruff 通过；git diff --check 通过。 |

扩大回归在最后的 fork 缓存清理补充前启动；该补充不改其他函数体，随后在最终代码上额外执行上述 63 项新增及 compact/fork 组合验证。各轮测试结果存在重叠，不累加为独立覆盖数量。

扩大回归命令：

```bash
env -u POSTGRES_DSN -u IOSQL_EXAMPLE_TEST_POSTGRES_DSN \
uv run --offline --no-project --python 3.11 \
  --with 'duckdb==2.0.0' --with 'pytest==8.4.2' --with 'arrow==1.3.0' \
  --with loguru --with 'typer==0.12.5' --with 'click==8.1.8' --with numpy \
  --with 'fastapi==0.121.0' --with httpx --with uvicorn --with 'psycopg2-binary==2.9.10' \
  python -m pytest -q tests \
  --ignore=tests/engines/postgresql \
  --ignore=tests/test_safe_migration_postgresql.py \
  --ignore=tests/test_partition_integrity_postgresql.py \
  --disable-warnings --tb=short -rs -p no:cacheprovider
```

最终补充组合沿用上述 uv 依赖，pytest 参数为：

```bash
python -m pytest -q tests/test_sqlite_review_regressions.py tests/test_plan093_review.py \
  -k 'sqlite_review_regressions or compact or fork' \
  --disable-warnings --tb=short -p no:cacheprovider
```

126 个跳过项为已废弃接口、PostgreSQL 实例/示例/认证配置、独立文件系统和 /proc、Playwright Chromium、Windows 原机及大小写/junction 条件。显式排除的 PostgreSQL 服务集成文件不计入这 126 项。未验证真实断电、Windows 原机和浏览器交互；Web API 测试包含在扩大回归内。已有 CLI 较新依赖兼容性待办 `TD-20261115-1621-001` 保持原范围，本次采用已知兼容的 Typer/Click 组合。

## 复审与计划偏差

- 初轮反例确认静默丢行、同批异构语句重复插入、失败后新增列意外留存等问题。后续修复和复审增加 REAL 比较/存储转换区别、物理声明覆盖契约别名、统计快照并发和 fork 锁清理；都在原比较/事务/资源范围内。
- 手动重复建表时，SQLite 会返回带引号的表名错误；修正 mysqlite 的既有表错误识别，保证幂等建表路径可用（`iosql/utils/mysqlite/table.py:42`）。
- DuckDB 2.0.0 下，既有资源测试观察桩的 `fetchone()` 留下未耗尽结果，导致观察动作阻断关系写入。完整基准 HEAD 独立副本也复现同一失败；将观察桩改为 `fetchall()`，原有断言全部保留，扩大回归通过（`tests/test_plan093_resources.py:143`）。
- 最后一轮重新覆盖十一条问题、物理/契约类型、事务边界、ASC/DESC 与全局 LIMIT、latest 分组/投影/请求顺序、compact 发布及重复键、分区裁剪、缓存的关闭/并发/fork 生命周期。该轮没有再修改实现或测试，必要验证均已通过，本次审查范围内未发现待修复的逻辑问题。

本次范围内无遗留事项。未执行 commit 或 push，未修改 GOAL.md 或现有业务数据库。
