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
- SQLite 普通查询使用已保存的分区边界定位 legacy 文件，不预扫描全列 MIN/MAX、时间范围和 COUNT；
  row segment 继续复用已有 manifest 统计。显式 `count_rows()` 只统计行数；
  `segments()`、布局/校验及 explain 等诊断仍可读取完整统计，explain 的裁剪决定与普通查询一致。
- **单 writer 保证**：写入路径与 `compact()` 共用 `_compact_lock` 串行化，消除 compact 后向旧 segment 插入的复活竞态；
  compact 与写互斥是预期吞吐代价。同一逻辑表建议单 writer，多线程读取可按需提高 `max_connections_per_file`。
- `compact()` 只整理当前可见 row segment，生成新的 sealed generation，并把旧 segment 标记为 `obsolete`；
  默认查询和 `segments()` 不扫描 obsolete。`repartition_to()` 写入新库，不要假设它会原地修改源库。
- PLAN-093：compact 使用临时磁盘外部排序；构建文件不可见，全部成功后在一个元数据事务中
  切换可见集合及发布记录。失败/进程退出保留旧可用集合；校验核对代成员，不拒绝合法重复主键。
  CLI 单表 `--replace-source` 保留未选中表；dry-run 列出替换和保留范围。表重命名通过
  持久 namespace_revision 让其他实例、进程丢弃旧文件句柄。
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
  会先校验 `mode`/`tables`/目标 URI，不创建业务临时目录，但可创建源协调锁文件；
  不连接目标，因此不校验目标连接、权限或既有表兼容性。本地源与目标不能相同或互相包含。
- 无序读取同样使用单只读游标与 fetchmany，避免 OFFSET 重扫；无序 LIMIT 逐文件下推并停止。
  latest 在文件内使用窗口查询筛出每组候选，Python 常驻内存随分组数增长；低层 upsert_all
  只读取输入主键对应的行，按输入顺序合并。SQLite 逻辑单列索引定义持久化，未来文件、compact
  以及迁移到 SQLite 的文件继承定义；不向无索引能力的 Parquet 目标承诺迁移索引。
- 显式 write(mode=...) 优先于表 conflict；strict 在创建任何数据文件前校验整批字段。
  未知投影列抛 BadArgument，物理缺列抛库级异常。统计无法证明与持久类型/比较亲和性一致时
  保守扫描，不能以裁剪改变行集合。segment ignore 批量查重并保持批内首行语义。
- Web 普通首页和快速计数不读取全列范围统计；局部主键布局的游标含物理文件身份，完整保留
  跨文件重复行。布局或排序变化使游标显式失效。PG 控制会话有界复用，每次调用仍重新取锁和
  检查 generation；关闭、断连、解锁失败及 fork 均有明确的清理路径。
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
- `insert_all` 按整批行列并集取值、缺列补 `NULL`；`upsert_all` 仅更新输入字段、缺字段保留旧值，并按参数上限拆页；
  `commit=False` 会保持事务，后续 `commit=True` 的写入或 delete 提交，关闭连接时未提交事务回滚，失败整体回滚。
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

## PostgreSQL 14+ 查询与诊断补充（PLAN-078）

- `query/latest/asof` 在数据库内完成筛选和排序；start/end/as_of 均包含边界。latest 按请求 symbol 顺序输出，同时间按剩余主键升序决胜。`latest(symbols=[])` 为空，`query(symbols=[])` 保留既有无筛选行为。
- 查询对象支持 `.to_list()` / `.one()` / `.iter_rows(batch_size=..., timeout_ms=...)` / `.iter_batches(...)` / `.explain(analyze=False, raw_plan=False)`。流式对象提前关闭会释放命名游标，显式写事务不会被读取擅自提交。同一 Table 的连接不应并发消费多个流。
- PG 的 `explain_select` 追加仅关键字 analyze/raw_plan；默认不执行、隐藏谓词常量，analyze=True 提供实际耗时与 buffer，raw_plan=True 可能含业务常量。QueryPlan 追加 engine/engine_plan/planning_time_ms/execution_time_ms/index_scans；PG 分区列表使用关系名，segment 列表为空。默认 scanned_partitions 为计划关系，ANALYZE 时只记录 Actual Loops>0 的关系。
- `create_index(column=None, order='asc', exist_ok=True, *, columns=None)` 支持复合 B-tree；与主键等价时复用。`drop_index(column=None, *, columns=None)` 仅删除当前表带 iosql 标记且定义匹配的二级索引；不能按名称误删用户索引或约束索引。
- 重开物理表验证声明列类型、主键顺序、分区形态和 RANGE 单列键；分区主键必须包含分区列。
- upsert_all 按输入主键增量合并，缺字段保留旧值，显式 None 写 NULL，重复主键按输入顺序合并。count_rows 是精确计数，可能全表扫描。
- 完整示例：`examples/15_postgresql_timeseries_query.py`。百万/千万行基准：仓库 `benchmarks/benchmark_postgresql_query.py`。

指定 symbols 的单 symbol 分组 latest 会在目录确认有效 symbol/time B-tree 前缀索引后使用 LATERAL LIMIT 1；无索引/多维分组/无 symbols 时使用 DISTINCT ON。

## 统一迁移与安全发布（PLAN-083）

- PLAN-089 新增 `Database.save_as()` / `Table.save_as()`：保留源数据、契约及对象 URI 的另存入口，复用 `migrate_to()` 的规范参数、默认值、结果和发布/锁机制。Table 的新接口使用 `tables`，没有旧 `target_table` 参数。已有 `migrate_to()` 同样保留源库；已有目标按 `mode` 合并，不自动清空。查询性能比较推荐新目标与 `verify="row-count"`，并在复制后使用相同条件及消费方式计时。

- `Database.migrate_to` 和 `Table.migrate_to` 都使用 `tables=None/list/dict`；对象决定全库/当前表范围，字典为 `{源名: 目标名}`。Table 的 `target_table=` 和旧第二位置目标名仍兼容，不能与 `tables=` 混用。`to_postgresql` 保留兼容，示例推荐 `migrate_to`。
- 两者共同提供 `specs=None`、`temp_dir=None`、`lock_timeout=30.0` 关键字参数。`specs={"ticks":{"partition":"duration(ts,every=1hour)"}}` 在转换时改变目标布局；值也可以是 `source.spec().with_partition(...)`，键始终是已选源表名。支持 partition=None 移除分区，支持 segment；不隐式改列/主键/时间单位。
- 已有目标必须兼容最终目标契约；`mode="replace"` 是主键冲突策略，不会清空或重建目标。`verify="row-count"` 要求各选中目标不存在或为空，实际行数在发布前验证；PG 还在正式事务里验证。
- 迁移全程持有源、目标库独占锁，协作 API 读写立即抛 `DatabaseBusy`。已消费但未关闭的读取流也持共享锁；关闭后才能迁移。同线程持共享锁时拒绝升级，其他线程/进程等待至 `lock_timeout` 抛 `MigrationLockTimeout`。
- 本地目标默认在目标文件系统的管理目录转换，再将完整目标视图放到目标 `__iosql_generations__/<任务ID>/`，原子切换父目录 `.iosql-locks/` 中的控制记录；目标根是挂载点时使用目标内的保留管理目录。PostgreSQL 目标保留本地源使用源库目录、远程源使用系统临时目录的默认规则。`temp_dir` 仍可显式覆盖，但不得位于目标库内。库 URI 不变。应通过 API 枚举路径，不能假定表一直在 `库根/表名`。
- PG 使用专用源快照/正式目标事务、暂存 schema 和独立协调连接；全表转换后一次 COMMIT。调用方未提交事务不得被迁移提交或回滚，先结束原事务。提交状态与数据共同提交，回复不确定时按任务锁和持久状态确认。
- `dry_run=True` 取得源共享锁，只统计源行数/目标契约，不连接或锁定目标，不创建业务临时目录。源需恢复时仅报告，不执行恢复。源协调锁文件可以产生。
- 发布前失败保留暂存供排查，正式目标不变；已决定提交的本地中断由下次读写恢复，不能绕过屏障。发布确认后立即解锁，清理在锁外；清理失败不撤销提交。主 API 之外的原始文件访问/旧版客户端不参与此协议。
- 结果保留旧字段，追加 rows_written、published、operation_id、source_spec、target_spec、partitions_created。rows_copied 是成功交给目标写入的源行数，ignore 时可能包含被忽略行；published 才表达正式发布。

数值时间列改变为 calendar 时保留 ms/s 单位及原值（含早期 epoch），PG 数值边界使用同一单位。既有已绑定 PG 对象跨发布只读恢复绑定；新打开对象仍需要显式声明。`partitions_created` 仅记录本次新增目标分区。

已完成本地提交的状态在被后续提交替换前按任务 ID 持久归档，旧代际清理不删除状态历史。buffered_writer 的 with 上下文持共享租约，append/flush 受维护屏障；flush 失败保留输入，不自动重试。迁移前应 flush 内存缓冲。PG 其他协作角色需具备读取控制 schema 状态的权限，迁移角色需管理该 schema；库不自动向 PUBLIC 扩大权限。

PLAN-084 审查修复：旧内容在仍持独占租约时移到稳定协调空间中的任务专属退休目录，锁外只清理该目录；快照过滤不跳过合法业务表目录。允许经过校验的 scaffold-only 空目标，当前未写入表的内存声明跨 generation 保留。秒级 calendar 查询只在分区范围比较处换算毫秒，保留已生成 manifest 和原值。ui/web 服务启动均不持常驻共享租约。Windows 库级协调使用原生共享/独占锁；未进行 Windows 原机及断电验证，不承诺 POSIX 目录 fsync 的同等持久性。

PLAN-085 补充：Windows 的最终 `.lock` 文件不跟随重解析点，按已打开句柄的属性拒绝所有重解析标签；持锁句柄不共享删除权限，阻止锁文件被删除或重命名替换。不能把这一检查扩大解释为全部父目录的安全保证，Windows 原机用例需在该平台单独执行。

PLAN-086 补充：锁身份规范化不改变实际文件访问路径，Windows 下真实库根、控制目录和代际路径保留大小写。快照根与每个子项在过滤和递归前通过 `lstat()` 检查，拒绝符号链接及带重解析属性的文件/目录，包括 junction；不依赖 `Path.is_junction()`，兼容 Python 3.9。
