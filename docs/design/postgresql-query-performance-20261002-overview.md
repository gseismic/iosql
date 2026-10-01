# PostgreSQL 高速时序查询设计

- 日期：2026-10-02
- 状态：评审修订并按 PLAN-078 完成实现与本机验证
- 范围：将 PostgreSQL 从迁移目标和基础查询后端，逐步提升为可诊断、可优化的时序查询后端

## 1. 动机

iosql 已支持 SQLite、Parquet 和 PostgreSQL，但 PostgreSQL 能力最初围绕 SQLite 数据迁移建立。目前 PostgreSQL 可以绑定表契约、批量写入、执行基础条件查询，也能使用 PostgreSQL 原生 RANGE 分区；时序便捷查询和查询计划诊断仍主要由 SQLite 引擎提供。用户把 PostgreSQL 用作在线主存储时，无法直接沿用熟悉的 `query()`、`latest()`、`asof()` 工作流，也难以判断一个慢查询究竟是没有裁剪分区、没有合适索引，还是查询本身需要扫描大量数据。

这次工作的动机是让用户能在 PostgreSQL 后端上**表达常见时序查询、让数据库执行查询、看见数据库采用的计划，并针对实际访问模式配置索引和分区**。目标不是承诺任意数据、硬件和查询都“高速”，而是让性能优化有可观察的依据，避免用户只能猜测。

“表超过一百万行就必须分表”不是可靠的设计规则。PostgreSQL 可以在大表上使用索引完成高选择性查询；读取大部分数据、返回大量宽行或执行大范围排序时，分区也无法消除数据处理成本。是否分区应由常用过滤条件、数据保留和维护方式决定；是否建索引应由查询计划与真实工作负载决定。分区只有在查询条件能约束分区键时，才能减少扫描范围；分区数量过多也会增加规划与会话开销。[PostgreSQL 表分区文档](https://www.postgresql.org/docs/current/ddl-partitioning.html)、[索引使用检查文档](https://www.postgresql.org/docs/current/indexes-examine.html)

因此，本设计把首要问题定义为 PostgreSQL 查询能力与诊断能力不完整，而非百万行表天然过慢。分区、索引、流式读取和计划检查是互补手段，各自解决不同瓶颈。

## 2. 实施前的实现与缺口

### 2.1 已有能力

- `Database` 可按 `postgresql://` URI 选择 PostgreSQL 引擎，使用 `db.table(..., spec=...)` 或声明参数绑定表契约。
- 分区表使用 PostgreSQL 原生 `PARTITION BY RANGE`。目前按单个分区键组织；duration、range 和常见 calendar 契约均可生成边界。
- 写入按分区键分组；目标子分区不存在时创建 `PARTITION OF`，再通过父表写入，由 PostgreSQL 路由记录。
- `select()` 支持参数化条件、列投影、排序和 limit；`iter_select_batches()` 使用 PostgreSQL 命名游标分批读取。
- 可创建单列索引；主键索引由表契约生成。

### 2.2 影响查询优化的缺口

| 领域 | 当前行为 | 对用户的影响 |
| --- | --- | --- |
| 时序查询 | PostgreSQL 没有 `query()`、`latest()`、`asof()` | 用户需要自行拼条件，无法沿用统一的时序查询入口 |
| 查询计划 | PostgreSQL 没有 `explain_select()`；查询对象也没有数据库计划 | 无法从 iosql 判断分区是否裁剪、索引是否使用、估算行数是否准确 |
| 结果读取 | `select()` 使用 `fetchall()`；另有分批流式接口 | 大结果集误用 `select()` 会让客户端内存随结果规模增长 |
| 二级索引 | `create_index()` 只接受单列；`drop_index()` 未实现 | 常见的“标的 + 时间范围”索引不能通过稳定的 iosql 接口表达 |
| upsert | `write(mode="upsert")` 走数据库写入路径；`upsert_all()` 会先全表读取、在 Python 合并、再写回 | 对大表调用 `upsert_all()` 的传输、内存和写入成本与全表规模相关 |
| 契约检查 | 重开 PostgreSQL 表需要重新绑定相同契约；当前已有表检查未完整比对物理类型、主键和分区键 | 契约与物理表漂移可能延迟到写入或查询时才暴露 |

代码位置：PostgreSQL 引擎在 [iosql/engines/postgresql/table.py](../../iosql/engines/postgresql/table.py)，当前能力边界见 [README.md](../../README.md#双引擎支持面)。查询计划对象 `QueryPlan` 已存在，但其字段主要描述 SQLite 文件分区和 segment 裁剪，不能直接代表 PostgreSQL 优化器计划。

仓库已有 PostgreSQL 集成查询和迁移测试，但目前没有 PostgreSQL 百万行查询基准。因此，分区粒度、复合索引和端到端延迟尚无仓库内实测证据。

## 3. 代表性用户场景

| 优先级 | 查询场景 | 主要性能手段 |
| --- | --- | --- |
| P0 | 查询单个标的在一个时间窗内的少量列 | 时间分区裁剪、`(symbol, ts)` B-tree 索引、列投影 |
| P0 | 查询一段时间内的全部标的 | 时间范围裁剪；由 PostgreSQL 选择索引扫描或分区顺序扫描 |
| P0 | 导出较大时间范围的有序数据 | 服务端游标、批次读取、数据库排序与 limit 下推 |
| P1 | 获取一个或多个标的截至某时点的最新记录 | 数据库端执行 latest/as-of 逻辑，避免全量结果传回 Python |
| P1 | 按 symbol、venue 或 timeframe 分组取最新记录 | 明确分组键和排序语义；用真实计划验证 SQL 实现及索引效果 |
| P1 | 不带时间条件的 symbol 查询 | 评估跨分区访问成本及 symbol 前缀索引是否合适 |
| P2 | 全表扫描或大范围聚合 | 投影、流式读取或数据库端聚合；分区不能替代扫描成本评估 |

## 4. 设计目标与非目标

### 设计目标

1. PostgreSQL 支持与 SQLite 语义一致的时序查询入口，同时保留 PostgreSQL 的原生执行和分区能力。
2. 查询过滤、排序、limit 和列投影尽可能下推到 PostgreSQL；大结果集沿用服务端游标，避免先整体物化到 Python。
3. 用户能查看 PostgreSQL 的结构化执行计划，区分分区裁剪、索引扫描、顺序扫描、排序和估算行数。
4. 能表达常见的复合 B-tree 索引，并使新建分区继承父表上的索引定义。
5. 建表和重开表时尽早检查 PostgreSQL 分区表的主键约束及契约与物理表的一致性。
6. 用代表性数据和查询负载建立 PostgreSQL 基准，避免把行数、分区或索引数量误当作性能结论。

### 非目标

- 不实现跨 PostgreSQL 实例或跨节点的分布式分片；原生分区仍位于同一个 PostgreSQL 数据库体系中。
- 不因达到某个固定行数就自动切分表，也不自动为所有列建索引。
- 第一阶段不追求 PostgreSQL 与 SQLite 全部维护 API 对齐；compact、segment、文件布局维护不属于 PostgreSQL 原生表的职责。
- 不提供脱离硬件、数据宽度、缓存状态和查询选择性的固定延迟承诺。
- 不改变既有公开接口的参数含义；新增能力遵守接口冻结约定，以新增方法或可选参数为主。

## 5. 对外接口设计

### 5.1 统一时序查询工作流

PostgreSQL 实现现有 SQLite 主接口，参数和结果语义保持一致：

```python
bars = db.timeseries(
    "bars",
    time="ts",
    pk=["symbol", "ts"],
    columns={"symbol": "text", "ts": "integer", "close": "float"},
    partition_every="1day",
)

rows = bars.query(
    symbols="AAPL",
    start=start_ms,
    end=end_ms,
    columns=["ts", "close"],
    order_by="ts",
).iter_batches(batch_size=10_000)

latest = bars.latest(
    symbols=["AAPL", "MSFT"],
    as_of=end_ms,
    columns=["symbol", "ts", "close"],
).to_list()
```

接口要求：

- `query()` 返回现有 fluent 查询对象，支持 `to_list()`、`iter_rows()`、`iter_batches()` 和 `explain()`。
- `latest()` / `asof()` 保持 SQLite 的 symbol、`by`、`as_of` 和结果顺序语义（同时间决胜规则见评审修订），由 PostgreSQL 执行 SQL 查询，不在 Python 中扫描整表。
- `where` 继续使用现有字典、列表、`Case` 或文本条件解析器的受支持形式；值使用参数绑定，标识符继续走校验和引用逻辑。
- `select()` 继续急切返回完整结果，以保持原有行为；文档明确推荐大结果集使用 `iter_select_batches()` 或查询对象的流式方法。
- 日期、时间戳、timezone 和 session 条件沿用现有 `TableContract` 语义，并对 PostgreSQL 的 text/date/timestamp 列分别验证边界转换。

### 5.2 PostgreSQL 执行计划

增加 PostgreSQL `explain_select()`，并让查询对象的 `.explain()` 调用同一实现。默认使用 `EXPLAIN (FORMAT JSON)`，不执行用户查询；可选 `analyze=True` 时运行 `EXPLAIN ANALYZE`，显示实际行数和耗时。`analyze=True` 必须显式传入，因为它会真实执行查询，可能扫描大量数据。[PostgreSQL EXPLAIN 文档](https://www.postgresql.org/docs/current/using-explain.html)

返回继续使用可序列化的 `QueryPlan`。通过新增可选字段保存 PostgreSQL 结构化计划与统计信息，例如 `engine_plan`、`planning_time_ms`、`execution_time_ms` 和 `index_scans`；不把 PostgreSQL 计划伪装成 SQLite 的 segment 裁剪统计。既有字段保持语义和兼容性。默认摘要不返回完整 SQL 或绑定参数列表；若提供原始 EXPLAIN JSON，需明确提示其中的谓词文本可能包含查询常量，并提供脱敏选项或由调用方显式选择原始计划。

计划至少需要让调用方看清：

- 计划中出现的分区关系名及分区裁剪结果；
- `Index Scan`、`Bitmap Index Scan`、`Seq Scan` 等访问路径和索引名称；
- 估算行数；开启 `analyze=True` 后的实际行数、执行时间和可用的 buffer 统计。

计划用于解释 PostgreSQL 的选择，不强行要求优化器必须选索引。若查询返回某分区中的大部分数据，顺序扫描可能是正确计划。

### 5.3 复合索引管理

保留现有单列调用，增加复合列声明，第一阶段只支持 PostgreSQL B-tree：

```python
table.create_index(column="symbol")
table.create_index(columns=["symbol", "ts"])
table.drop_index(columns=["symbol", "ts"])
```

列顺序属于索引定义的一部分。第一阶段不自动为所有字段建索引，不自动推断唯一约束，也不把索引策略写入 `TableContract`；用户显式调用索引管理接口。对 PostgreSQL 原生分区父表创建的索引应覆盖现有子分区，之后创建的子分区也应得到相同索引定义。[PostgreSQL 分区索引说明](https://www.postgresql.org/docs/current/ddl-partitioning.html)

对“标的 + 时间范围”场景，优先测量 `(symbol, ts)` 的组合效果。若其正好匹配主键顺序，应复用主键索引，不再重复创建等价索引。其他字段组合根据 `EXPLAIN` 和基准结果决定，不为追求“索引齐全”牺牲写入吞吐和存储空间。

### 5.4 契约和分区约束

- PostgreSQL 原生分区表的主键或唯一约束必须包含全部分区键列。iosql 在发出 DDL 前校验此限制并给出明确修复信息；PostgreSQL 文档说明了该限制的原因是分区内索引不能单独保证跨分区唯一性。
- 重开已有表时，校验声明列的数据类型、主键列、分区方式和分区键。不能仅凭“存在同名表”和“该表是分区表”就视为契约相符。
- PostgreSQL 的契约当前由调用方在重开时重新绑定；本设计先维持这一行为，不把契约持久化迁移混入查询性能范围。
- 继续使用原生 RANGE 分区。时序查询需要包含时间边界才可能裁剪时间分区；只按 symbol 查询时，可能仍要访问多个分区。

## 6. 内部实现方向

1. 复用 `QuerySpec` 和现有 where AST，将 symbol、time、date、session 等条件编译为 PostgreSQL 参数化 SQL；尽可能复用已有 SQLite 端的语义测试，而不复用其物理文件裁剪实现。
2. 复用当前命名服务端游标实现支撑 `iter_rows()` / `iter_batches()`。批次消费完成或迭代器关闭时关闭 portal 并结束读取事务；保留现有 timeout 机制。
3. `latest/asof` 由数据库端完成分组和排序。优先考察 `DISTINCT ON` 与 window function 两种 SQL 形态，以结果语义和查询计划基准决定实现；不将全表读入 Python 后取最新值。
4. 索引在父表上创建，让 PostgreSQL 维护对应子索引；索引名称继续处理 PostgreSQL 标识符长度限制。索引删除应只定位 iosql 能确认归属的索引，不能按相似名称误删用户自行创建的对象。
5. 将 `upsert_all()` 改为数据库端增量合并，保持其“只更新输入中提供的字段，未提供字段保留原值”的语义；按列集合分组或使用等价的批量 SQL，避免全表 `SELECT` 和全表回写。完整行替换仍由 `write(mode="upsert")` 既有路径处理。
6. `count_rows()` 保持精确计数语义，但文档提醒它可能扫描多个分区，不作为高频在线统计缓存。

## 7. 分区与索引决策原则

- 对常见查询由时间范围限制、并且需要按时间淘汰历史数据的表，使用时间 RANGE 分区；周期根据实际数据量、查询窗口和运维节奏选择。
- 对不按时间过滤或数据量不大的表，可以使用普通非分区表；分区不是百万行阈值触发的必选项。
- 当查询的过滤条件包含分区键时，利用分区裁剪减少候选数据；分区键之外的过滤依赖索引或 PostgreSQL 顺序扫描选择。
- 不创建过多细粒度分区来替代索引，也不在不能裁剪分区的查询中宣称“分区后一定更快”。分区数量和保留策略应进入基准与生产观测。
- 不把 PostgreSQL 本机 RANGE 分区描述成分布式分片；跨节点扩展属于独立架构决策。

## 8. 基准和验收方式

性能目标按查询类型与数据集报告，不设脱离环境的统一毫秒门槛。实现阶段新增 PostgreSQL 基准，至少覆盖 100 万和 1000 万行，并报告 schema、数据宽度、分区数、索引、PostgreSQL 版本、硬件、缓存状态及批次大小。

必须比较的工作负载：

1. 平表与时间 RANGE 分区：同一条窄时间窗查询，验证分区裁剪是否减少访问关系。
2. 分区表有/无 `(symbol, ts)` 索引：单 symbol 时间窗查询和排序查询。
3. 单时间窗全部 symbol 与仅 symbol 条件：区分能裁剪的查询和跨分区查询。
4. `latest/asof` 少量 symbol 与大批 symbol：验证数据库端运算、返回正确性和查询计划。
5. 大结果流式读取：验证首批延迟、总吞吐和 Python RSS 随 `batch_size` 有界增长。
6. 全表扫描：作为扫描型基线，不把它与少量行的索引查询直接比较。

验收标准：

- PG 与 SQLite 同类 `query/latest/asof` 的边界条件、排序和返回字段语义一致。
- `explain_select()` 默认不执行查询，返回有效 JSON 计划；`analyze=True` 明确返回实际执行指标。
- 对带时间范围且分区边界兼容的查询，计划能识别 PostgreSQL 实际扫描的分区；不以“必须 Index Scan”作为硬性断言。
- `iter_batches()` 不调用 `fetchall()`，内存占用不随完整结果行数线性增长。
- 新建/重开分区表时，错误的主键分区键组合、物理列类型或分区键应在绑定阶段抛出类型化异常。
- 基准报告展示平表、分区、索引组合的实际结果和适用限制；性能结论可由同版本环境复跑。

## 9. 实施阶段建议

### 阶段一：查询可用且可解释

实现 PostgreSQL `query()`、`latest()` / `asof()`、`explain_select()` 与统一 `QueryPlan` 扩展；大结果流式读取走当前命名游标。补契约关键字段和 PostgreSQL 分区约束的校验。

### 阶段二：索引和增量更新

扩展单列索引 API 支持复合 B-tree 索引；实现安全的索引删除；将 `upsert_all()` 改成数据库端增量合并并保持字段合并语义。

### 阶段三：测量、调整和运维

新增百万/千万行查询基准，根据真实计划优化 latest SQL、分区周期建议和索引文档。若测试显示首次写入建分区影响连续写入，再设计显式预建分区能力；不提前增加不必要的分区管理 API。

## 10. 风险与边界

- 索引提升部分读查询性能，但增加写入、更新成本和磁盘使用；由用户按访问模式声明。
- `EXPLAIN ANALYZE` 会执行查询，宽范围或全表查询可能消耗明显资源；默认只生成不执行的计划。
- 分区裁剪依赖条件与分区边界相容；对分区键应用不利于裁剪的表达式时，优化器可能无法剪枝。
- PostgreSQL 优化计划受版本、统计信息、配置和数据分布影响，测试时应记录环境并先保证统计信息有效。[PostgreSQL 索引使用检查](https://www.postgresql.org/docs/current/indexes-examine.html)
- 当前 PostgreSQL 契约重开时需要调用方提供；本设计改善物理 schema 校验，但不解决契约元数据跨进程持久化。
- 需要在实施计划中确认支持的 PostgreSQL 最低版本，并在该版本上覆盖原生分区、复合索引和 JSON EXPLAIN 行为。

## 11. 参考资料

- 当前 PG 能力表：`README.md` 的“双引擎支持面”。
- PostgreSQL 引擎：`iosql/engines/postgresql/table.py`。
- 时序查询契约：`iosql/spec.py` 中的 `QuerySpec` 与 `QueryPlan`。
- SQLite 查询参考：`iosql/engines/sqlite/table.py` 中的 `_TableQuery`、`query()`、`latest()` 和 `explain_select()`。
- [PostgreSQL 18：表分区](https://www.postgresql.org/docs/current/ddl-partitioning.html)。
- [PostgreSQL 18：检查索引使用](https://www.postgresql.org/docs/current/indexes-examine.html)。
- [PostgreSQL 18：使用 EXPLAIN](https://www.postgresql.org/docs/current/using-explain.html)。

## 变更记录

- 2026-10-02：初稿。记录 PostgreSQL 后端的能力现状、百万行性能判断依据、查询和索引设计，以及基准与分阶段实施建议。

## 12. 评审修订后的执行契约

- 最低 PostgreSQL 版本为 14，真实集成与基准使用独立临时数据库，避免触碰业务表。
- 提取与物理文件无关的时序筛选/分组参数助手；SQLite、Parquet 与 PostgreSQL 共用，不通过继承 SQLite Table 获得 PG 查询能力。
- start/end/as_of 均包含边界；date 的毫秒/秒范围沿用 TimeColumn；原生 DATE 以契约时区取日期，TIMESTAMP 取契约时区墙上时间，TIMESTAMPTZ 绑定带偏移的 ISO 时间字符串（兼容共享 where AST 的标量值）。calendar text 使用对应格式（compact/ISO），不在 WHERE 对分区列包函数。
- latest 使用 DISTINCT ON 在数据库内决胜，时间 DESC NULLS LAST，时间相同时按剩余主键 ASC NULLS FIRST。SQLite 原契约未规定同时间非唯一分组记录的决胜；新增显式稳定规则用于 PG，同时间对照仅在主键唯一决定结果时断言一致。无 symbols 时结果按最新时间降序；指定 symbols 时优先按输入 symbol 顺序，其余组仍按最新时间降序。重复 symbols 保留重复输出，与 SQLite 一致。
- EXPLAIN 默认只规划。QueryPlan 追加 engine、engine_plan、planning_time_ms、execution_time_ms、index_scans 等可选字段；现有 candidate/scanned/skipped_partitions 保存 PG 关系名，segment 字段为空。默认 engine_plan 仅保存结构与数值指标，where=None；raw_plan=True 显式允许谓词、输出表达式及其中常量。analyze=True 附带 BUFFERS；实际扫描仅统计 Actual Loops>0 的节点，规划关系不能假称已经访问。
- query 与 latest 的 explain 必须解释各自真实执行 SQL；命名游标统一承载两种查询，可逐批读取和提前 close。
- 索引保持单列调用，追加 keyword-only columns。列顺序与 order 决定定义；使用系统目录比对有效 B-tree、列/方向/非表达式/非部分索引，等价主键不重复建。新建索引打归属标记；删除要求归属标记、当前表 OID 与定义同时匹配，并保护约束索引，缺索引幂等。不能仅凭同名或相似名字删除。
- upsert_all 的重复输入主键按出现顺序字段合并，后值覆盖、缺字段保留、显式 None 写 NULL；先在输入规模内合并，再按字段集合批量 ON CONFLICT。只触碰输入主键，不扫描已有整表。write(upsert) 保留原职责。
- 绑定失败恢复内存契约；检查列类型、主键顺序、RANGE 单列分区键和双向分区形态一致性。PG14+ 通过系统目录提供上述证据。
- 基准固定合成数据、schema 与数量，100 万及 1000 万行均运行全部六类负载；记录数据库端 EXPLAIN ANALYZE 与客户端流式耗时，RSS 使用独立进程采样，区分客户端批次内存与服务端排序成本。缓存说明使用预热后重复查询，不宣称冷缓存。

评审发现的旧实现全表 upsert、弱 schema 校验和缺少 PG 查询入口均在本次实施范围；分区预建 API 仍按实测决定。

### 基准驱动的 latest SQL 修订

初轮实测表明 DISTINCT ON 在 1000 个指定 symbol 的千万行历史上仍需大量排序。最终实现：默认 symbol 分组且目录确认存在有效、非部分、非表达式的 (symbol,time) B-tree 前缀索引时，使用 VALUES + LATERAL ORDER BY time DESC / pk ASC LIMIT 1；每个 symbol 在服务端取一行。无相应索引、多维 by 分组或未指定 symbol 时保留 DISTINCT ON，避免无索引的重复全表扫描。两种 SQL 都保持相同字段和输出顺序，explain 描述实际选用的形态。

### 日期与文本精度修订

原生无偏移日期/时间按契约时区解释；DATE、TIMESTAMP 与 TIMESTAMPTZ 写入先规范化，再按同一时钟路由分区。负时区日期和小时墙上时间不应先当 UTC 而移到错误的物理分区。calendar text 的查询格式由已存值的精度决定，按月分区仍能按日筛选，日期时间文本的 date 条件覆盖完整自然日；支持定长 compact 或无偏移 ISO，同列统一格式，带偏移时间使用 timestamptz。
