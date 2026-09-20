# iosql 接口契约与设计偏好

> 接口已定稿冻结（PLAN-054，长期稳定）：不再改参数名、默认值、方法职责；新增能力只能以"新方法/新参数"叠加。
> 本文在需要判断"某个写法是否合规、会不会踩坑"时阅读；日常使用只需 SKILL.md。

## 参数契约

- 参数词汇表唯一拼写：`pk` / `columns` / `every` / `timezone` / `between` / `mode`；
  别名永久保留：`primary_key` / `fields` / `partition_every` / `tz` / `between_and` / 契约侧 `conflict`。
- 缩写只允许行业级 `pk`，其余参数一律全称。
- 结果对象字段冻结，`to_dict()` 是唯一序列化入口，新字段只能追加。
- 复杂 where 优先使用受支持的 dict/list/`Case` 或文本 where，不要拼接原始 SQL。

## 废弃参数（只加不减，永久保留）

`partition_kind` / `time_unit` / `*_rows_per_file` / `group_by` / `select_all` / `delete(None)` 的
DeprecationWarning 永久保留，不要计划移除，也不要在新代码里使用：

| 废弃写法 | 迁移到 |
|----------|--------|
| `partition_kind='calendar'` + `time_unit='date'` | `partition=Partition.calendar(...)` |
| `target/min/max_rows_per_file` | `segment=Segment.rows(...)` |
| `select_all(...)` | `select(..., to_dict=True)` |
| `latest(group_by=...)` | `latest(by=...)` |
| `delete(None)` | `delete_all()` 或 `truncate(confirm=True)` |
| `upsert_all(where=...)` | 仅关键字传入且已废弃；按 pk 在全表范围合并 |

## 行为约定

- `columns={"col": "type"}` 中声明的列类型在自动建表时优先生效；未声明的列由数据推断。
- `TableSpec/TableContract` 是表契约唯一来源，避免隐式依赖第一次写入推断出的结构。
- **导出面收缩**：`from iosql import ...` 只导出工厂 `Partition.*` 和结果类型；
  `DurationPartition`/`CalendarPartition`/`RangePartition` 需从 `iosql.partition.duration` 等内部模块导入（顶层导入会 ImportError）。
- **symbol_column 显式化**：`db.table()` 默认 `symbol_column=None`；未声明时 `query(symbols=)` / `latest(symbols=)`
  抛带修复指引的 `BadArgument`。需要 symbol 语义就显式声明 `symbol_column="symbol"`（`timeseries()` 默认 `"symbol"`）；
  `latest(by=...)` 显式分组不受影响。
- `select()` 双引擎统一默认 `to_dict=True` 返回 dict 行；依赖 tuple 行为的旧代码必须显式 `to_dict=False`。
- 间隔词汇统一由 `iosql.partition.duration.parse_interval_ms()` 解析：`Partition.duration(every='1s')` 与
  `timeseries(partition_every='1s')` 行为一致，支持别名 + 任意 `Nunit`，以及 `Nmonth`（按 30 天）/`Nyear`（按 365 天）近似；
  bool 等非法值抛 `PartitionSpecError`（`BadArgument` 子类）。
- `segment` 支持 `max_rows`/`target_rows`/`min_rows` 三级参数和可选 `stats_columns`；
  partition/time/order/session 字段会自动保留 min/max 统计。
- **单 writer 保证**：写入路径与 `compact()` 共用 `_compact_lock` 串行化，消除 compact 后向旧 segment 插入的复活竞态；
  compact 与写互斥是预期吞吐代价。同一逻辑表建议单 writer，多线程读取可按需提高 `max_connections_per_file`。
- `compact()` 只整理当前可见 row segment，生成新的 sealed generation，并把旧 segment 标记为 `obsolete`；
  默认查询和 `segments()` 不扫描 obsolete。`repartition_to()` 写入新库，不要假设它会原地修改源库。
- manifest 引用文件缺失时读取直接抛 `UnsafeOperation`（不隐式重建）；`validate()` 会以 `orphan_segment_file`
  警告列出孤儿文件；compact 可安全复用被孤儿占用的目标文件名。需要释放旧文件时先 `validate(deep=True)`，
  再用 `cleanup_obsolete(dry_run=False)`。
- 连接预算：主 API 默认 `max_open_partitions=5`、`max_connections_per_file=1`、
  `max_process_connections=32`、`max_ordered_read_connections=128`，分别限制单表分区缓存、
  单文件连接池、进程真实连接总数和单次有序归并分区数；不要依赖提高 `ulimit -n`，也不要直接修改私有字段。
  分区 LRU 使用租约保护并发访问。长期进程必须使用上下文管理器或显式 `db.close()`。
- 大结果集读取优先用 `iter_select_batches()` / `iter_batches()` / `query(...).iter_rows()` /
  `query(...).iter_batches()` 或 CLI `export --batch-size`；SQLite 带全局 `order_by` 时使用
  分区只读游标和堆归并惰性输出，不先走 `select()` 全量物化，但受
  `max_ordered_read_connections` 与 `max_process_connections` 约束。`select()` 的
  `order_by`+`limit` 会按 segment 下推后归并（内存有界）。
- 大文件导入优先用 `iosql import --batch-size` 读取 JSONL/CSV；JSON 数组兼容但不适合数 GB 文件。
- SQLite→PostgreSQL 迁移优先用 `Database.migrate_to()` / `to_postgresql()`；按 `batch_size` 逐批读取 SQLite 物理文件，
  不会一次性载入全库。生产迁移先 `dry_run=True` 预览，再 `verify="row-count"` 校验。`migrate_to(dry_run=True)`
  会先校验 `mode`/`tables`/目标 URI 且无落盘副作用；SQLite 源迁 SQLite 目标时目标不能等于/嵌套源库。
- PostgreSQL 也可以作为迁移源写回 SQLite；重新打开 PostgreSQL 逻辑表时需要提供同一份 `TableContract`
  （`db.table(name, spec=spec)`），见 `examples/12_sqlite_postgresql_roundtrip.py`。PG 流式导出使用命名游标（单语句快照），
  无序翻页不会跳行/重复。
- PostgreSQL native partition 下，`duration(...)` / `range(...)` 使用数值边界；`calendar(...)` 支持
  `text` / `date` / `timestamp` 目标列，text 日期列不要混用 ISO 与 compact 格式。
- 异常体系统一使用 `IosqlError` 子类（`BadArgument`、`TableSpecConflict`、`SchemaConflict`、`UnsafeOperation` 等）；
  `PartitionSpecError` 继承 `BadArgument`，损坏/旧 manifest 缺字段也走该异常族；`IosqlBadArgument` 已移除，统一用 `BadArgument`。
- 代码结构按层理解：`iosql.core.query` 是通用查询 AST/where 解析；`iosql.engines.sqlite` / `iosql.engines.postgresql`
  是引擎实现；旧 `iosql.query` / `iosql.sqlite` / `iosql.postgresql` 包已删除。
- 测试要求：必须覆盖写入结果、查询结果和必要的 `explain()` 裁剪信息；每个测试用独立 `tmp_path`；
  修改核心行为时至少跑相关 sqlite 契约测试、partition 测试和 mysqlite 测试。

## PostgreSQL 引擎差异

- `delete(where)` 参数化执行并返回删除行数；`where=None` 警告并转发 `delete_all()`，
  `[]`/`{}`/`""`/`0`/`False` 等 falsy 非 None 值抛 `BadArgument`（不会退化为全表删除）。
- 列类型走封闭白名单（`blob→BYTEA`、`datetime→TIMESTAMP`，未知类型抛 `BadArgument`）；
  已有物理表缺契约列、或声明分区但表未分区时抛 `SchemaConflict`。
- 字符串 `fields` 只接受 `*` 或标识符列表；含 SQL 语法直接抛 `BadArgument`；
  `select()` / `iter_select*()` 拒绝未知 kwargs。
- `insert_all` / `upsert_all` 按整批行列并集取值、缺列补 `NULL`，并按参数上限拆页；
  `commit=False` 会保持事务，后续提交写入或关闭时提交，失败整体回滚。
- `order_by` 的 NULL 排序与 SQLite 一致：ASC → `NULLS FIRST`，DESC → `NULLS LAST`。
- `upsert_all(..., where=...)` 已废弃（忽略并告警）；`calendar(...)` 的 `timestamptz` 边界带 UTC 偏移；
  `table_parts()` 返回排序后的子分区关系名。

## 低层 mysqlite 约定

定位是"SQL 完备工具"（第三方库心智）：

- `insert_all(commit=False)` 显式拒绝（延迟提交与连接池归还语义冲突）；`insert()/executemany(commit=False)`
  与 `auto_commit=False` 下的直写同样拒绝，需要事务用 `db.transaction()`。
- `select(limit=0)` 按 SQL 语义返回 0 行；错误路径统一回滚并转换异常；建表冲突抛类型化 `DuplicateColumn`/`PrimaryKeyNotInColumn`。
- `Database(':memory:')` 被拒绝（池内连接各自独立）；`insert_all` 缺列交由 SQLite 求值表达式默认值；
  `wal=False`/`timeout` 真实生效；DDL 后缓存自动失效（外部 DDL 需手动 `table.invalidate_cache()`）。
- 无内置 rename/drop_column/backup——用原生 SQL 或连接 API 完成（见 `examples/mysqlite-scenarios/`）。

## 反模式（避免）

- ❌ 直接传文件路径给主 API（如 `Database("db/market.iosql")`）→ 必须用 `sqlite://` 前缀
- ❌ `from iosql import DurationPartition` → 顶层已不导出分区类，用 `Partition.duration(...)` 工厂
- ❌ 对已存在表重复声明不同契约 → 用 `db.table("name")` 获取引用而非重新声明
- ❌ 在写入前未声明 `pk` 或写入行缺主键字段 → 会抛 `BadArgument`（不会退回 rowid 自增），每行必须补齐主键
- ❌ 给 `select()` / `iter_select*()` 传 `symbols`/`start`/`end`/`date`/`session` → 抛 `BadArgument`；业务过滤用 `query()` / `explain_select()`
- ❌ 未声明 `symbol_column` 的普通表调用 `query(symbols=)`/`latest(symbols=)` → 先 `symbol_column="symbol"` 显式声明或改用 `by=`/`where=`
- ❌ 依赖旧版 `select()` 默认返回 tuple → 现默认 dict 行，需要 tuple 显式 `to_dict=False`
- ❌ 用 `delete(None)` → 改用 `delete_all()` 或 `truncate(confirm=True)`
- ❌ 假设 `repartition_to()` 会修改原库 → 它只写新库
- ❌ 拼接原始 SQL 做 where → 用 dict/list/`Case`/文本表达式
- ❌ 忽略 `WriteResult` 返回值 → 检查 `rows_seen` vs `rows_written`
- ❌ 吞掉 buffered_writer 异常退出时的 RuntimeWarning → 提示的是未 flush 行数，意味着数据未落库
- ❌ 忽略测试数据隔离 → 每个测试用独立 `tmp_path`
- ❌ 用 `insert_all()` 替代 `write()` → `insert_all` 是低层 API，不自动使用表契约
- ❌ mysqlite `insert_all(commit=False)` / `Database(':memory:')` → 见上文 mysqlite 约定
