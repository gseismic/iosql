# Parquet 后端设计

> 安装策略更新（2026-10-18）：DuckDB 已纳入 iosql 默认依赖，普通安装即可使用 Parquet；
> 引擎仍在打开 `parquet://` 后端时才导入 DuckDB。具体变更见
> [PLAN-082](../dev/PLAN-082-parquet-default-dependency.md)。

## 目标与使用方式

新增 `Database("parquet://db/market.iosql")`，通过既有 `table()`、
`timeseries()`、`write()`、`select()`、`query()`、`latest()` 和流式接口读写
本地 Parquet 数据。支持 duration/calendar/range 分区、行数 segment、
schema 演进、重开、计数、校验、整理和 SQLite/PostgreSQL 双向迁移。

典型场景包括历史行情归档、批量回灌、按时间查询、按列投影、跨分区排序、
晚到数据追加和已有 SQLite 库转换。高频逐行修改不是优先使用场景。

## 存储与内部接口

- 默认依赖 `duckdb>=1.1`，普通安装即可使用 Parquet；DuckDB 仍按需导入。
- 表级 `meta.sqlite3` 保存现有表契约和 segment manifest；业务文件位于
  `data/*.parquet`。manifest 添加 `storage_engine="parquet"` 防止以错误后端打开。
- Parquet Table 继承现有目录表实现，复用契约、分区路由、where AST、查询计划、
  最新截面、segment 和迁移；覆盖物理读写、流式游标、校验和文件枚举。
- 读取通过 DuckDB 的 Parquet scan，并使用参数绑定，保留过滤和投影下推。
  按契约投影旧文件中缺失的列为 NULL，schema 演进不重写其他分区。
- 单文件写入在临时 DuckDB 表中完成主键检查和修改，然后写入相邻临时
  Parquet 文件（ZSTD），fsync 后原子替换目标文件，再更新 manifest。
  现有表级文件锁串行化同机写入与 compact。读取期间使用共享文件锁，
  阻止写入替换正在扫描的文件；读取租约让 close/cleanup 等待或拒绝活跃游标。

## 行为边界

- 追加、upsert、replace、ignore 和删除会重写受影响的物理文件；建议批量写入并
  用 `Segment.rows` 控制文件大小。segment 的既有 upsert/replace 限制继续适用。
- 原子性单位是单个数据文件；跨文件写入及数据与 manifest 之间没有统一事务。
  读取流存活期间，同表写入会等待；持有读取流的线程直接写同表会报错，
  应先耗尽或关闭流。跨文件读取没有统一快照，迁移建议先停写。
- `commit=False`、SQLite PRAGMA、持久化 SQL 索引及 `aggregate_by_bucket`
  明确拒绝，避免暗示无法提供的能力。数据库目录重命名暂不支持，表重命名支持。
- 本轮提供 Python 后端与 CLI 迁移入口；Web 工作区仍按原有 SQLite 目录发现逻辑工作。
- 支持的列类型与现有规范类型一致：integer/float/text/blob 及其常见别名。
  不接受任意 SQL 类型表达式，主键不能为空。
- 本地受 iosql 管理的目录是数据库入口；任意现有 Parquet 文件不会自动被接管。

## 技术依据

DuckDB 官方说明 Parquet scan 支持过滤和列投影下推，COPY 可以写出 Parquet：
https://duckdb.org/docs/stable/data/parquet/overview 。
Python API 提供 `read_parquet`、参数化 SQL 和 `fetchmany`：
https://duckdb.org/docs/stable/clients/python/reference/ 。

## 验证

用独立临时目录验证真实 Parquet 文件、重开、四种写入模式、分区/segment 裁剪、
NULL/二进制、schema 演进、流式全局排序、删除、compact、校验、损坏/缺失文件、
错误后端、迁移往返和失败时原文件保持可读。运行相关 SQLite/分区/mysqlite 回归。
