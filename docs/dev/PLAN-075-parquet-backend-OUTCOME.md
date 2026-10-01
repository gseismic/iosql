# PLAN-075 实施结果：Parquet 后端

## 交付内容

- 新增 `iosql.engines.parquet.Table`，通过 `Database("parquet://...")` 使用。
  增加 `iosql[parquet]` 可选依赖 `duckdb>=1.1`，默认安装不导入 DuckDB。
- 复用目录后端的表契约、时序语义、分区路由、二级查询计划和维护逻辑。
  SQLite 层只增加文件后缀、存储身份的扩展点，默认值保持 SQLite 行为。
- 业务文件是真实的 ZSTD Parquet；表契约、领域元信息和 segment manifest
  保存在 SQLite。空表契约可重开，错误后端或普通业务目录会被拒绝。
- 支持 duration/calendar/range 分区、行数 segment、四种写入模式、缓冲写入、
  schema 演进、投影、文本/AST where、最新截面、全局有序流式查询、删除、
  深度校验、compact、obsolete 清理、布局报告、表重命名、重分区和迁移。
- 读取使用 DuckDB Parquet scan；文件中未出现的演进列按契约投影为 NULL。
  写入在临时 DuckDB 表中检查主键，临时 Parquet 经 fsync 后原子替换。
  失败的单文件修改保留原文件，并清理临时文件。
- 读取共享锁与租约保护活跃游标；同机同表写入/compact 串行化。
  同线程持有读取流时写入会明确报错，跨 Database 实例也能识别。
- CLI `migrate` 支持 SQLite ↔ Parquet，并保留 PostgreSQL 目标入口。
  补充 README、API/CLI 使用参考及 `examples/14_parquet_backend.py`；
  示例与仓库技能副本同步。

## Review 与修复

1. DuckDB 1.3.2 的 COPY 路径不能使用 `?` 参数，改用 `write_parquet()`，
   避免路径拼接 SQL，也兼容依赖下限 DuckDB 1.1.0。
2. 二进制/非有限数值统计不写入 JSON，禁用这些列的统计裁剪；数据仍正常存储。
3. 普通表在 manifest 引用文件缺失时拒绝读取及写入，防止悄悄创建空文件。
4. segment 统计取实际存储值，修复数值转文本/浮点转整数后的错误裁剪。
5. 数值分区键在转换会改变路由或丢失精度时提前拒绝，避免跨边界漏查。
6. 全局排序对齐 NULL 的 ASC/DESC 顺序，流式读取不调用急切 `select()`。
7. 读取租约纳入 close/cleanup 生命周期；共享文件锁防止扫描期间替换文件。
8. 同库与嵌套目录迁移在不同 URI 后端组合下也提前拒绝；重分区按批流式复制。
9. 查询绑定/损坏文件错误转换为 iosql 异常，超时中断保留 TimeoutError。
   未实现的低层约束参数不会静默忽略。

## 验证

- 新增 `tests/test_parquet_backend.py`：34 项通过。覆盖真实 Parquet 文件头尾、
  契约重开、全部分区类型、segment 裁剪、冲突模式、NULL/二进制/schema 演进、
  流式投影排序、删除/compact/cleanup、缺失/损坏文件、写入失败、后端身份、
  可选依赖、双向迁移与 dry-run、CLI、跨实例读写锁、资源限制及类型转换。
- 在当前 DuckDB 1.3.2 环境及隔离的 DuckDB 1.1.0 环境验证新增后端。
  最终 34 项测试在 DuckDB 1.1.0 隔离环境通过；不需要 PyArrow。
- 无 DuckDB 的隔离环境能够导入 iosql；访问 Parquet 表时提示安装
  `iosql[parquet]`，不创建业务目录。
- `examples/14_parquet_backend.py` 验证 Parquet → SQLite → Parquet 往返。
- `ruff check` 检查新增后端、测试、示例与身份识别文件通过；
  `git diff --check` 通过；新增示例和 README 与技能副本字节一致。
- `uv build --wheel` 成功，检查 wheel 包含 Parquet 模块与 optional extra 声明。

针对性测试命令：

```bash
uv run --isolated --no-project --with 'duckdb==1.1.0' --with pytest --with arrow --with typer --with loguru python -m pytest tests/test_parquet_backend.py -q
```

相关回归：SQLite 时序与分区表契约、partition、partition_string、mysqlite、
migration、rename、PLAN-062 core、PLAN-072 fixes、CLI、user_api_unify、distribution，
合计 **310 通过、6 失败**。在未修改的 HEAD `86bfca6` 导出副本中，逐项重现了
这 6 项相同失败：

| 测试 | 既有失败原因 |
| --- | --- |
| `test_query_iter_rows_order_by_streams_without_full_select` | 测试中的 monkeypatch 未接收新增的 deadline 参数。 |
| `test_process_budget_reclaims_idle_connections_across_pools` | 组合运行时全局连接预算为 32，测试期待 3。 |
| `test_postgresql_select_iter_batches_and_count_are_parameterized` | FakeCursor 缺少 close。 |
| `test_postgresql_database_migrate_to_sqlite_batches_rows` | FakePagingCursor 缺少 close。 |
| `test_numeric_text_buckets_and_overflow` | 测试按单个 JSON 解析当前 NDJSON 图表响应。 |
| `test_web_candle_order_pagination_and_sort_validation` | 测试按单个 JSON 解析当前 NDJSON 图表响应。 |

## 支持边界与工作区状态

Parquet 的追加、修改和删除重写受影响的物理文件，建议批量写入并配置 segment。
沿用 segment 的 upsert/replace 限制和分区列必须属于 upsert 主键的规则。
原子性单位是单个数据文件；跨文件写入、数据与 manifest 更新没有统一事务。
读取流存活期间同表写入会等待，跨文件读取没有统一快照。

Web 工作区发现、其余 CLI 命令、数据库目录 rename、PRAGMA、持久化索引、
`commit=False`、`aggregate_by_bucket` 和低层 default/not_null/unique/index
参数暂不支持。未连接真实 PostgreSQL 数据库进行 Parquet 往返集成测试。

所有变更保留在工作区，未创建 git commit。用户原有 `.zcodeignore` 未改动。
