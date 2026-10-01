# PLAN-078 PostgreSQL 时序查询与性能诊断结果

- 日期：2026-10-02
- 状态：实现、评审、本机验证与基准完成
- 计划：`docs/dev/PLAN-078-postgresql-query-performance.md`
- 设计：`docs/design/postgresql-query-performance-20261002-overview.md`

## 设计评审与实现

1. 保留现有公开参数含义，固定 start/end/as_of 包含边界；latest 按请求 symbol 顺序返回，重复 symbol 保留重复结果，同时间按剩余主键升序决胜。原 SQLite 未规定同时间非唯一分组的决胜，不宣称此类输入跨引擎完全相同。
2. 提取 `iosql/core/query/timeseries.py` 共用条件、分组参数及错误包装。SQLite/Parquet 保留其物理布局，PG 使用独立 `PostgreSQLQueryMixin` 和查询对象，支持 query/latest/asof、to_list/one/spec、iter_rows/iter_batches 与 explain。
3. WHERE、排序、投影、limit、分组均在 PG 内执行。通常使用 DISTINCT ON；有有效 symbol/time B-tree 前缀索引且指定 symbols 的单分组 latest 使用 VALUES + LATERAL LIMIT 1。目录无相应索引时避免重复全表扫描，多维分组继续 DISTINCT ON。VALUES 显式绑定物理 symbol 类型。
4. 复用命名游标，用 fetchmany 逐批读取。提前关闭释放 portal 和自有读事务；显式写事务内读取/索引管理不擅自提交。服务端取消时保留原异常，避免 CLOSE 的错误覆盖超时。主键列禁止 NULL，可使用索引原生正反顺序；可空字段保持 ASC NULLS FIRST / DESC NULLS LAST。
5. QueryPlan 追加 engine、engine_plan、planning_time_ms、execution_time_ms、index_scans。EXPLAIN 默认不执行，结构化摘要隐藏谓词、SQL 和参数；analyze=True 提供实际耗时/buffer，raw_plan=True 显式保留原计划。默认 scanned_partitions 表示计划关系，ANALYZE 时仅记录 Actual Loops>0 的关系，不填造 PG segment 统计。
6. 增加复合 B-tree 和安全删除。目录核对列顺序、方向、方法、有效性及表达式/部分索引，复用等价主键；父表索引覆盖已有和未来子分区。受管索引标记、当前表归属和实际定义一致时才能删除，保护约束和外部用户索引。旧版本未打标的索引不会被自动接管删除。
7. upsert_all 仅合并输入主键，按字段集合分页 ON CONFLICT。未提供字段保留原值，显式 None 写 NULL，重复主键按输入顺序合并；整体事务失败回滚，不读取/重写已有整表。保留该方法旧的返回行为和 where 废弃警告。
8. 建表前检查分区主键包含分区列；绑定检查实际 UDT、主键顺序、分区形态和 RANGE 单列键，失败恢复原内存契约。包括零列物理表的类型化报错。
9. 原生 DATE/TIMESTAMP/TIMESTAMPTZ 写入与查询使用同一时钟，修复负时区日期和小时墙上时间的路由。calendar text 查询精度由值格式决定，按月分区仍能按日查询；支持定长 compact/无偏移 ISO，带偏移时间使用 timestamptz。
10. 更新 README 与仓库 iosql skill；新增 `examples/15_postgresql_timeseries_query.py`，同步 skill 示例与目录说明。新增可重复运行的百万/千万行基准及 JSON/中文报告。

## 本机验证

使用本机已安装的 PostgreSQL **14.22**，最低支持版本真实验证。以 `initdb -A trust -U postgres` 在 `/tmp/iosql-plan078-pg.*` 创建独立数据目录，绑定 `127.0.0.1:55478`。已有系统 PostgreSQL 18 实例未用于本次测试。无需安装或重置密码，因此没有安装任务需要委派 luna。

Python 使用现有 3.12.8 环境，通过 `uv run --no-project --python /home/lsl/miniconda3/bin/python python ...` 执行。

### 功能、回归与示例

- 相关全套回归：**486 passed、11 skipped**，92.32 秒。覆盖 PG、SQLite、Parquet、partition、mysqlite 与迁移。11 项跳过来自已有的旧字符串分区格式废弃测试。
- 最终 PG/迁移回归：**152 passed**，2.35 秒；与上一套有重叠，不叠加当作独立用例总数。包含新增 **51** 项真实 PG 参数化用例。
- 新增覆盖：PG/SQLite 条件、时间边界、session、排序/投影、latest/by/asof 对照；空与重复 symbols；两种 latest SQL 的结果和真实计划；NULL 顺序；数值 symbol；日/月/小时和负时区；EXPLAIN 默认不执行、脱敏/原始 JSON/ANALYZE/buffer；复合索引继承和主键复用；漂移与错误分区主键；字段保留、NULL、重复更新、整批回滚；流关闭、消费预算、服务器 statement_timeout 与显式事务。
- 真实示例运行成功，确认单分区查询、请求顺序的截至时点截面、字段保留与实际执行指标。
- 修改文件的 `ruff check` 和 `git diff --check` 通过；示例与 skill 副本 `cmp` 一致。
- 自审修复了索引 DDL 提交调用方事务、CLOSE 覆盖超时、原生日期条件进入共享 AST 的类型、TEXT 精度依赖分区周期和 VALUES 字符串类型推断等问题。

关键命令（先设置独立测试实例的 POSTGRES_DSN）：

```bash
uv run --no-project --python /home/lsl/miniconda3/bin/python python -m pytest -q tests/engines/postgresql tests/test_plan062_pg.py tests/test_plan056_fixes.py tests/test_migration.py
uv run --no-project --python /home/lsl/miniconda3/bin/python python examples/15_postgresql_timeseries_query.py
uv run --no-project --python /home/lsl/miniconda3/bin/python python benchmarks/benchmark_postgresql_query.py --repeats 3
```

### 全量回归中的已有失败

早期执行整个 `tests` 得到 815 passed、13 skipped、6 failed。修正了已过时的 SQLite 有序流测试回调（缺 deadline 参数），并将 PG NULL 排序的 SQL 断言改为测试可空 price 列，避免依赖主键不可能出现的 NULL。

其余四项在修改前提交 **3e42dba** 的独立工作树复现：

| 用例 | 修改前的失败证据 |
| --- | --- |
| `test_plan062_web.py::TestCountBudget::test_filtered_count_budget` | 假设 1ms 查询必定超时，实际可能 200；独立复跑仍失败，属于不稳定计时断言 |
| `test_plan072_fixes.py::test_numeric_text_buckets_and_overflow` | 把当前 NDJSON 图表响应按单个 JSON 解析，报 Extra data |
| `test_plan072_fixes.py::test_web_candle_order_pagination_and_sort_validation` | 同上，测试仍期望旧的桶聚合 JSON |
| `test_web.py::TestReadOnlyApi::test_chart_line` | 同上，测试仍期望旧 JSON x/y 结构 |

这些 Web 用例不作为本次 PG 实施的验收证据，也没有把全量测试报告称为全绿。

## 基准产物与验收

- 脚本：`benchmarks/benchmark_postgresql_query.py`。
- 原始结构化数据：`benchmarks/results/postgresql-query-20261002.json`。
- 表格报告：`benchmarks/results/postgresql-query-20261002.md`。
- 100 万、1000 万行，平表/32 个日分区 × 有无 symbol/ts 索引，共 8 组。schema 六列，主键 (ts,id)，1000 个 symbol、约 32 天；固定合成输入，ANALYZE 后预热一次，小查询 3 次中位数。
- 覆盖单 symbol 窄时间窗、排序窗口、全部 symbol 时间窗、仅 symbol、latest/asof 少/多 symbol、多维分组、全列扫描以及三列全量有序流式导出。
- 每组有数据库端实际计划与客户端耗时；流式读取分别用 1000/10000 行批次，独立进程核对完整行数和全局顺序，记录首批、总吞吐、RSS。RSS 包含启动高水位，不含 PG 服务端缓存和排序内存。
- 环境：本机 PG14.22、Python3.12.8、NVMe KINGSTON SNVS1000G、ext4；CPU/RAM、PG 配置、物理行宽、表/索引字节数保存在 JSON。

最终代码复测完成，8 组组合的结果、流批次、裁剪与 latest SQL 形态核对通过：

| 千万行负载 | 平表无复合索引 | 分区无复合索引 | 平表有复合索引 | 分区有复合索引 |
| --- | ---: | ---: | ---: | ---: |
| 单 symbol 窄时间窗（ms） | 1.038 | 0.900 | 0.082 | 0.230 |
| latest 1000 个 symbol（ms） | 3302.80 | 4725.37 | 8.03 | 8.54 |
| (symbol,venue) 多维 asof 1000 组（ms） | 2348.26 | 5142.16 | 2243.56 | 5180.87 |

- 分区时间窗实际扫描 **1/32** 个分区；有索引 latest 的计划包含 Nested Loop、Limit 和 Index Scan，无索引 latest 的计划包含 Unique 和排序。
- 百万/千万行的 RSS 增量分别约 **12–14 / 17–18 MiB**，没有按结果总行数线性增长。启动高水位影响 RSS，不能据此承诺 batch_size 对峰值内存的精确倍率。
- 千万行三列有序导出的吞吐约 **124.6–134.9 万行/秒**，首批约 **56.5–384.0 ms**；六列全量扫描约 **9.1–9.5 秒**。
- 实测样本行约 66 字节；千万行平表/分区表无复合索引约 1031/1033 MiB，加索引后约 1332/1335 MiB。完整八组大小、配置及结构化计划保存在报告。
- 两种布局原本都有 (ts,id) 主键索引，分区裁剪不代表每种负载都更快。多维 asof 仍需秒级历史扫描，且本次只有一个 venue；报告明确该限制。

## 完成核对

| 计划要求 | 完成证据 |
| --- | --- |
| 评审设计并固定接口语义 | 设计第 12 节与共享时序条件模块 |
| query/latest/asof 与服务端流式读取 | 新 PG 查询模块、51 项真实参数化集成用例、真实示例 |
| EXPLAIN/ANALYZE、脱敏、裁剪与索引诊断 | 真实 PG 计划测试及 8 组基准 JSON |
| 复合索引、增量 upsert、契约校验 | 真正数据库目录/事务/分区测试与最终 152 项回归 |
| 其他引擎与迁移兼容 | 486 项相关回归通过；四项原有 Web 失败单独记录 |
| 百万/千万行基准与文档 | 8 组完整结果、README、仓库 skill 与镜像示例 |
| 中文提交并推送 | 本结果与计划一并纳入提交，推送后核对远端提交 |

## 边界

- PG 契约继续由调用方重开时绑定，没有增加契约持久化、分布式分片、segment 维护或自动分区阈值。
- 精确 count_rows、全表扫描和无索引/多维 latest 仍可能处理大量数据；性能结论依赖查询选择性、统计、缓存与机器。
- 同一 Table 使用单连接，不应在未关闭流时启动会结束该事务的其他操作；并发业务使用不同 Table/连接。
- 原生单列 RANGE 使用 iosql 自动生成的直接子分区，本次不引入多层子分区管理。

## 工作区与提交

- 用户原有 `.zcodeignore` 不纳入提交。
- 初始 master 已领先 origin/master 2 个提交，保留既有历史；本次使用单独中文提交，列出计划、结果及具体变更。
- 验证结束后已删除基线工作树，停止并清理本次独立 PG14 实例与临时数据目录；系统 PostgreSQL 实例和用户数据保留。
