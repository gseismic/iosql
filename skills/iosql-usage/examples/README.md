# iosql 示例

先按要解决的问题选择示例。每个 Python 示例都是自包含脚本，可以单独复制运行；需要先安装 `iosql` 和对应后端依赖，PostgreSQL 示例还需配置数据库连接。下列顺序依据时序数据应用中日常读写、数据维护和专项诊断的相对使用场景估计，**不是实际访问统计**。文件名中的数字保留历史路径，不代表推荐顺序。

## 常用：建表、写入和查询

| 场景 | 示例 | 动机 |
| --- | --- | --- |
| SQLite 时序入门 | [01_quickstart_timeseries.py](01_quickstart_timeseries.py) | 创建时序表，写入数据并按标的、时间窗口查询。 |
| PostgreSQL 时序入门 | [01_quickstart_postgresql.py](01_quickstart_postgresql.py) | 连接 PostgreSQL，直接写入并查询；示例表保留以便查看。 |
| PostgreSQL 环境变量密码 | [01_postgresql_env_password.py](01_postgresql_env_password.py) | 从 `PGPASSWORD` 提供密码，写入和查询普通表。 |
| Parquet 入门 | [14_parquet_backend.py](14_parquet_backend.py) | 写入 Parquet，重新打开库后查询。 |
| 已有库增量写入 | [09_existing_database_append.py](09_existing_database_append.py) | 重开本地库并复用持久化契约，跳过重复主键。 |
| 条件查询 | [01_quickstart_table.py](01_quickstart_table.py) | 显式使用 `table()` 和分区，按条件查询并查看裁剪计划。 |
| 日期事件 | [02_calendar_events.py](02_calendar_events.py) | 用自然日期和日历分区保存、查询事件。 |
| 只更新部分字段 | [03_partial_upsert.py](03_partial_upsert.py) | 用 `upsert_all()` 更新指定字段，保留未提供的字段。 |
| 缓冲批量写入 | [03_buffered_ingest_and_query_plan.py](03_buffered_ingest_and_query_plan.py) | 缓冲多次写入，并查看窗口查询的分区裁剪。 |
| 大结果集逐行读取 | [13_streaming_ordered_query.py](13_streaming_ordered_query.py) | 全局排序后惰性读取，避免把整个结果装入内存。 |
| 金融行情 | [finance/01_kline_bars.py](finance/01_kline_bars.py) | 保存 OHLCV，查询时间窗口和最新截面。 |

## 按需：分区、迁移与性能

| 场景 | 示例 | 动机 |
| --- | --- | --- |
| 跨日交易日 | [02_calendar_events_table.py](02_calendar_events_table.py) | 使用 `session_column` 查询属于同一交易日的夜盘和日盘事件。 |
| 分区内控制文件大小 | [05_partition_segments_smoke.py](05_partition_segments_smoke.py) | 观察逻辑分区与物理 segment 的两级裁剪。 |
| 晚到数据 | [06_row_segment_late_data.py](06_row_segment_late_data.py) | 没有逻辑分区时按行数滚动 segment，并查询晚到数据。 |
| 布局诊断 | [04_layout_report_and_repartition.py](04_layout_report_and_repartition.py) | 查看布局建议，将数据重分区到新库。 |
| 处理已有库的布局 | [10_existing_database_repartition.py](10_existing_database_repartition.py) | 读取已有表契约，离线重分区到独立目标库。 |
| 命令行重分区 | [07_partition_string_repartition_cli.py](07_partition_string_repartition_cli.py) | 在 CLI 中使用分区表达式并写入新库。 |
| 历史库诊断 | [08_doctor_time_first_suggestions.py](08_doctor_time_first_suggestions.py) | 用 `doctor` 查看时间优先的分区建议。 |
| SQLite → Parquet | [16_sqlite_to_parquet.py](16_sqlite_to_parquet.py) | dry-run 后迁移到 Parquet 并验证行数。 |
| SQLite → PostgreSQL | [11_sqlite_to_postgresql_migration.py](11_sqlite_to_postgresql_migration.py) | dry-run 后迁移到 PostgreSQL 并验证行数。 |
| SQLite ↔ PostgreSQL | [12_sqlite_postgresql_roundtrip.py](12_sqlite_postgresql_roundtrip.py) | 演示跨后端往返与 PostgreSQL 表契约重绑定。 |
| PostgreSQL 查询诊断 | [15_postgresql_timeseries_query.py](15_postgresql_timeseries_query.py) | 查看时间窗口的原生计划与复合索引。 |
| 比较两种存储 | [18_save_as_compare_engines.py](18_save_as_compare_engines.py) | 保留独立副本，核对结果后比较相同查询的耗时。 |
| SQLite 目录库备份 | [17_safe_migration_and_paths.py](17_safe_migration_and_paths.py) | 使用公开的 `iosql backup` 命令创建备份并查询。 |

`Segment.rows(...)` 示例使用 append-first 语义，不演示跨 segment 的 `upsert`/`replace`。上述字段级更新示例使用不分 segment 的表。

## 金融与低层工具

[finance/README.md](finance/README.md) 按场景列出 K 线、orderbook、财报和新闻/公告。涉及期货夜盘或跨时区交易日时，使用 `session_column` 和 `query(session=...)`。

[mysqlite/README.md](mysqlite/README.md) 是直接操作单个 SQLite 文件的入门示例；[mysqlite-scenarios/README.md](mysqlite-scenarios/README.md) 是增删改查、查表信息和备份等专项操作。通常先选择上面的 iosql 主 API。

## 运行

在仓库根目录安装开发版本，选择一个脚本执行：

```bash
python -m pip install -e .
python examples/01_quickstart_timeseries.py
python examples/03_partial_upsert.py
python examples/14_parquet_backend.py
```

Parquet 是默认依赖。产生本地持久数据的脚本，每次在脚本所在目录的 `runtime/` 下创建独立目录并保留结果；可在确认后自行清理。`17` 使用自动清理的临时目录。`09` 如果设置了 `IOSQL_EXISTING_URI`，会向指定的已有表写入示例记录；请只在允许写入的测试库上运行。`10` 的目标必须是独立的新库。

### PostgreSQL 示例

需要 PostgreSQL 14+、已存在的测试数据库及建表权限：

```bash
python -m pip install -e ".[postgresql]"
export POSTGRES_DSN='postgresql://user@localhost:5432/testdb'
IFS= read -r -s -p 'PostgreSQL 密码: ' PGPASSWORD
printf '\n'
export PGPASSWORD
env -u PGSERVICE python examples/01_quickstart_postgresql.py
env -u PGSERVICE python examples/01_postgresql_env_password.py
unset PGPASSWORD
```

`POSTGRES_DSN` 中可只放用户名、主机、端口和数据库名；PostgreSQL 驱动可从环境变量 `PGPASSWORD` 读取密码。交互输入不会将密码写入 shell 命令历史。已有名为 `password` 的变量时，可以用 `export PGPASSWORD="$password"` 映射。也可以从 URI 中省略用户名，如 `postgresql://localhost:5432/testdb`，并设置 `PGUSER`。这些是 [PostgreSQL libpq 环境变量](https://www.postgresql.org/docs/current/libpq-envars.html)，适用于读写和迁移。

`PGPASSWORD` 仍是明文，部分系统可能允许其他用户读取进程环境。另一种标准方式是使用 [PostgreSQL 密码文件](https://www.postgresql.org/docs/current/libpq-pgpass.html)：预先准备文件，每行格式为 `主机:端口:数据库:用户名:密码`，字段中的 `:` 和 `\` 需要用 `\` 转义；设置 `PGPASSFILE` 指向该文件，并确保 `PGPASSWORD` 已取消。Unix 上文件不能允许组和其他用户访问，通常设为 `0600`；文件本身仍以明文保存密码。

例如，配置好 `~/.pgpass` 后，可以执行：

```bash
export POSTGRES_DSN='postgresql://user@localhost:5432/testdb'
chmod 0600 "$HOME/.pgpass"
export PGPASSFILE="$HOME/.pgpass"
unset PGPASSWORD
python examples/01_quickstart_postgresql.py
```

URI 中显式提供的密码优先于上述默认来源。`postgresql://user:!password@...` 会把 `!password` 当作字面密码，不会读取同名环境变量。

时序入门示例创建或打开 `iosql_example_ticks`；环境变量示例创建或打开普通表 `iosql_example_env_quotes`。两者都保留数据，`mode="ignore"` 会跳过重复主键。环境变量示例要求设置 `PGPASSWORD`，并拒绝含密码或 `service` 参数的 URI 以及 `PGSERVICE`，避免 service 配置中的密码覆盖演示的密码来源。验证密码时应使用需要密码认证的测试库；无密码认证的库无法证明密码被使用。使用 `PGPASSFILE` 时可运行时序入门示例。时序示例输出的 `rows_seen` 是提交的记录数，包含跳过的行；`ts` 使用毫秒，时间窗口包含起止边界。重开 PostgreSQL 表时应再次声明相同契约；完整示例见 `12`。

`11` 和 `12` 在未设置 `POSTGRES_DSN` 时只准备本地源库并 dry-run；设置后会原子创建带随机后缀的 PostgreSQL 目标表并保留结果。可分别用 `IOSQL_POSTGRES_TARGET_TABLE` 和 `IOSQL_POSTGRES_ROUNDTRIP_TABLE` 指定新的目标表名；已有或并发创建的同名关系会被拒绝。发生错误时已预留的表可能保留，应检查后再处理。`15` 使用随机表并在结束时清理，演示查询计划时，小数据集可能选择顺序扫描。

本机已安装 PostgreSQL 14+ 时，也可以启动独立的本机测试实例：

```bash
bash examples/start_postgresql_test.sh
source examples/runtime/postgresql-test.env
python examples/01_quickstart_postgresql.py
python examples/15_postgresql_timeseries_query.py
```

启动脚本使用 `127.0.0.1:55478`、本机无密码测试身份 `iosql` 和 `examples/runtime/postgresql-test/` 数据目录。其他安装位置可设置 `POSTGRES_BIN`；重启电脑后重新运行启动脚本。PostgreSQL 示例读取 `POSTGRES_DSN`，不依赖旁边的配置文件；环境变量密码示例还要求 `PGPASSWORD`。停止本机测试实例时，使用实际安装版本的 `pg_ctl`，例如：

```bash
/usr/lib/postgresql/14/bin/pg_ctl -D examples/runtime/postgresql-test/data -m fast -w stop
```

`17` 在临时目录演示备份并自动清理。`18` 用 20,000 行比较查询耗时；首次查询不代表系统冷缓存，实际性能仍需用业务数据和负载测量。
