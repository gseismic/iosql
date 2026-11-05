# 迁移接口统一设计

- 创建时间：2026-10-18 10:38（Asia/Shanghai）；安全方案修订时间：2026-10-18 10:44；参数统一修订时间：2026-10-18 10:53。
- 模型：Codex（GPT-6；环境未提供更具体的模型 ID）。
- 状态：已实施并完成验证（WORKTREE，2026-10-18 12:43）；旧调用保留兼容。证据和最终结论见 docs/dev/PLAN-083-model-unknown-safe-migration-OUTCOME.md。
- 初始分析范围：WORKTREE，基准提交 `b752db62fa34c31a880d4bf8b3cdcdce011ea48c`。实施基线更新为 `7a0f87fd1289eb551896b9d38c509cd2c4edc311`，实施计划见 `docs/dev/PLAN-083-model-unknown-safe-migration.md`。
- 相关工作区内容：未跟踪的 `examples/16_sqlite_to_parquet.py` 及其 skill 副本；已有修改的 README、示例索引和 Parquet 指南。本轮期间另有 Parquet 默认依赖相关并行改动，当时仅做设计；后续已获实施授权，需要同步示例和文档。实现仍位于 WORKTREE，未提交。
- 设计依据：用户指定的 `api-design-zh`；现有迁移实现、示例和 PLAN-054 的长期兼容约定。
- 2026-11-05 17:13 更新：环境未提供具体模型 ID（`model-unknown`）；本地目标的默认临时工作区调整为目标文件系统，取代本文历史上的源目录默认值，分析与验证见 `docs/plans/PLAN-20261105-003-model-unknown-migration-workspace.md`（WORKTREE，基准提交 `e9dee736a579f2319af91b2f022f1b0706968dcd`）。

## 1. 推荐方案

`Database` 和 `Table` 都使用 **`migrate_to(target_uri, tables=None, ...)`**。`tables` 在两处始终表示“源表选择及源名到目标名的映射”，接收对象决定允许的源表范围：

- `Database`：范围为当前库；`tables=None` 迁移全库。
- `Table`：范围仅为当前表；`tables=None` 迁移当前表，显式选择时也只能引用当前表。

目标 URI 表达迁往哪种存储，参数名、参数类型、默认值与调用方式保持一致：

```python
with Database("sqlite://db/market.iosql") as source:
    result = source.migrate_to("parquet://db/archive.iosql")
```

正式迁移推荐显式开启行数校验：

```python
with Database("sqlite://db/market.iosql") as source:
    result = source.migrate_to(
        "parquet://db/archive.iosql",
        verify="row-count",
    )
    print(result.rows_copied, result.verified)
```

只迁一张或几张表：

```python
source.migrate_to(target_uri, tables=["bars", "events"])
source.migrate_to(target_uri, tables={"bars": "archived_bars"})

bars = source.table("bars")
bars.migrate_to(target_uri)
bars.migrate_to(target_uri, tables={"bars": "archived_bars"})
```

迁往 PostgreSQL 时，将目标替换为 `postgresql://...`；迁回 SQLite 时使用 `sqlite://...`。每种入口在不同后端上都使用相同参数、预览、校验和结果读取方式。

“迁移”定义为向目标复制表契约和数据，保留源库。调用是同步的，会阻塞至完成或抛出异常。默认不会清空目标，目标已有数据时遵循 `mode`。

根据用户补充的安全要求，**目标实现默认全程独占源库和目标库，先在临时位置完成转换、合并和校验，再发布，最后解锁**。正式目标不能逐批暴露结果。临时工作目录优先放在原始本地库的保留目录中；跨文件系统时增加目标侧临时目录。完整发布和恢复协议见第 10 节。

用户已明确优先采用简单的全程禁读写方案，文件锁解除要快。因此本轮选择全程独占，不设计转换期间继续读旧目标、读写双屏障或增量追赶。

关键决定：

1. 保留 Database/Table 两个作用域，统一方法名和参数词汇；Table 不会借助 `tables` 扩大为其他表或整库。
2. 新代码统一使用 `tables=`；旧单表 `target_table=` 保留兼容，归一化成相同名称映射后进入共同流程。
3. 推荐可选参数以关键字传入；兼容层保留旧位置调用及返回类型。共同流程负责库级独占锁、临时转换、校验、发布和恢复。
4. 两个迁移示例使用相同的源表建模、预览、执行和基础查询写法。环境准备保留各自所需的步骤。

## 2. 现状与问题依据

| 已确认的现状 | 代码依据 | 对设计的影响 |
| --- | --- | --- |
| 示例 11 调用 `to_postgresql()`，示例 16 调用 `migrate_to()` | `examples/11_sqlite_to_postgresql_migration.py:66`、`examples/16_sqlite_to_parquet.py:30` | 同一种用户意图出现两个推荐动词。 |
| `to_postgresql()` 校验目标协议后直接转调 `migrate_to()` | `iosql/database.py:915` | 可以复用现有入口完成命名统一。 |
| Database 迁移已支持全库、表名、列表和源到目标的字典映射 | `iosql/database.py:929` | 将列表与字典映射复用于 Table，保持 `tables` 在两个作用域中的含义一致。 |
| Database 在预览前校验 mode、目标 URI 和本地自迁移；SQLite/PG 单表入口没有相同校验，Parquet 另加了一层 | `iosql/database.py:802`、`iosql/engines/sqlite/table.py:1600`、`iosql/engines/postgresql/table.py:1407`、`iosql/engines/parquet/table.py:535` | 入口统一还需要共用前置校验。 |
| Python 在迁移后比较目标总行数；CLI 对本地目标另行检查已有表 | `iosql/engines/sqlite/table.py:1660`、`iosql/engines/postgresql/table.py:1467`、`iosql/cli.py:1445` | 同一个 `row-count` 在不同入口的失败时机和空表处理不同。 |
| `rows_copied` 累加每批源数据长度，没有使用 `write()` 的结果 | `iosql/engines/sqlite/table.py:1640`、`iosql/engines/postgresql/table.py:1446` | `ignore` 等模式下不能把这个字段理解为新增行数。 |
| PG 表契约保存在实例中，重开后 `spec()` 不会自动恢复；往返示例显式绑定契约 | `iosql/engines/postgresql/table.py:466`、`iosql/engines/postgresql/table.py:511`、`examples/12_sqlite_postgresql_roundtrip.py:92` | 示例不能暗示 PG 重开后所有时序语义已自动恢复。 |
| 示例 11 用 `table()` 建模、`select()` 读取；16 用 `timeseries()` 建模、`query()` 读取 | `examples/11_sqlite_to_postgresql_migration.py:27`、`examples/11_sqlite_to_postgresql_migration.py:91`、`examples/16_sqlite_to_parquet.py:61`、`examples/16_sqlite_to_parquet.py:77` | 应统一迁移示例的叙述方式，避免把建模层次差异表现成后端差异。 |
| 当前文件锁在表目录内，SQLite 主要用来串行化写入；Parquet 的读取租约也使用该表锁 | `iosql/engines/sqlite/table.py:365`、`iosql/engines/parquet/table.py:146` | 现有锁不能直接提供整个目标库发布期间的读取屏障。 |
| 库身份检查不接受任意额外子目录；表枚举已排除 `__` 前缀 | `iosql/utils/storage_identity.py:101`、`iosql/utils/storage_identity.py:132` | 原始目录内的临时空间必须作为保留管理目录被显式识别。 |

`Database.migrate_to()` 与 `Table.migrate_to()` 是两个不同方法。用户希望统一的是参数形式：当前前者使用 `tables`，后者使用 `target_table`。本设计保留对象作用域，以同一种 `tables` 映射表达选表和改名，不另设整库/选表方法。

`table()` 是通用建模入口，`timeseries()` 是时序快捷入口；两者各有用途。本设计将两个迁移示例都设为同一张时序表，将基础查询统一为 `select()`，以便直接比较迁移部分。

## 3. 按使用频次安排复杂度

以下为依据现有示例和迁移工作流作出的定性排序，没有用户遥测数据。

| 场景 | 预计频次 | 推荐表达 | 使用者需要了解的参数 |
| --- | --- | --- | --- |
| 已有 SQLite 库归档到 Parquet，或迁到 PostgreSQL | 高 | `db.migrate_to(target_uri)` | 目标 URI。 |
| 只迁移其中几张表 | 高 | `db.migrate_to(target_uri, tables=["bars", "events"])` | `tables`。 |
| 正式执行前查看规模和表名 | 高；每次迁移通常一次 | 同一调用加 `dry_run=True` | 预览不访问目标。 |
| 核对完整复制的行数 | 高；正式迁移通常一次 | 加 `verify="row-count"` | 所选目标表不存在或为空。 |
| 已拿到表对象，只迁这一张 | 中 | `bars.migrate_to(target_uri)` | 无须重复表名。 |
| 改目标表名或批量映射名称 | 中 | Database/Table 都使用 `tables={源: 目标}` | 显式名称映射；低频改名可以重复源名以保持一致。 |
| PostgreSQL 数据迁回本地 | 中低 | `db.migrate_to("sqlite://...", tables=["pg_bars"])` | 当前 PG 源需要先绑定契约。 |
| 大库控制每批内存和写入规模 | 按调用配置的频次较低；执行时每批使用 | `batch_size=50_000` | 批次上限。 |
| 向已有目标合并，处理主键冲突 | 低 | `mode="ignore"` 或 `mode="upsert"` | 目标后端支持情况、幂等性边界。 |
| 迁移时改分区布局 | 常用 | `table.migrate_to(uri, specs={源名: {"partition": 新布局}})` | 不传则保留源布局；repartition_to 继续兼容。 |

先展示目标 URI，再逐步展开表选择、校验和批次；入门示例不要求构造迁移配置对象或适配器。

## 4. 用户接口

### 4.1 两个作用域，共用参数契约

以下为统一后的推荐签名；输入参数名称、顺序、类型和默认值完全相同。返回类型沿用现状：Database 为聚合结果，Table 为单表结果。旧 `target_table` 和旧位置调用由兼容层另外接收，见第 8 节。

```python
# Database：当前库中的源表，None 表示全库。
def migrate_to(
    self,
    target_uri: str,
    tables: list[str] | dict[str, str] | None = None,
    mode: str = "append",
    batch_size: int = 10_000,
    verify: str | None = None,
    dry_run: bool = False,
    *,
    specs: dict[str, TableContract | dict] | None = None,
    temp_dir: str | None = None,
    lock_timeout: float = 30.0,
) -> MigrationResult: ...

# Table：仅当前表，None 表示当前表。
def migrate_to(
    self,
    target_uri: str,
    tables: list[str] | dict[str, str] | None = None,
    mode: str = "append",
    batch_size: int = 10_000,
    verify: str | None = None,
    dry_run: bool = False,
    *,
    specs: dict[str, TableContract | dict] | None = None,
    temp_dir: str | None = None,
    lock_timeout: float = 30.0,
) -> TableMigrationResult: ...
```

两个作用域推荐相同的三种形式：`None` 使用接收对象的完整范围，列表选择源表并保留名称，字典选择源表并指定目标名。

```python
source.migrate_to(target_uri)                                   # 当前库全部表
source.migrate_to(target_uri, tables=["bars", "events"])         # 选择多表
source.migrate_to(target_uri, tables={"bars": "archived_bars"})  # 选择并改名

bars = source.table("bars")
bars.migrate_to(target_uri)                                     # 当前表
bars.migrate_to(target_uri, tables=["bars"])                     # 同上，显式选择
bars.migrate_to(target_uri, tables={"bars": "archived_bars"})    # 当前表改名
```

范围规则：

- `tables=None` 表示接收对象的完整源范围。Database 在独占锁中枚举当前库；Table 的范围始终只有当前表。任一被选源表缺失契约即在写正式目标前报错。
- 空列表、空字典报 `BadArgument`，不能当作 `None`。Table 显式传入列表或字典时，所有源名必须是当前表；其他源名或多表选择均报错，不回退到数据库作用域。
- 列表中的重复源名沿用去重行为；多个源表映射到同一目标名应提前拒绝。
- Database 指定多个表是一次迁移任务，统一加锁、暂存、校验和发布。逐表循环调用 Table 方法不具备多表共同发布语义。

例如 `bars.migrate_to(target_uri, tables={"events": "archive"})` 抛 `BadArgument`，提示当前对象是 `bars`，选择其他表请使用 Database。

### 4.2 参数语义

| 参数 | 取值和默认值 | 明确约定 |
| --- | --- | --- |
| `target_uri` | 必填，`sqlite://`、`parquet://`、`postgresql://`；兼容 `sqlite3://` | 指定目标；按 URI 选引擎。参数及报错输出应避免泄露密码。 |
| `tables` | 两个作用域均接受，默认 `None`；推荐列表或字典 | `None` 使用当前对象全部源表；列表保留源名；字典为源名→目标名。Table 不能选择当前表以外的源名。 |
| `mode` | 默认 `"append"`；另支持 `"ignore"`、`"upsert"`、`"replace"` | 透传目标 `write()` 的主键冲突策略，不能解释为清空或重建目标。 |
| `batch_size` | 默认 `10_000`，正整数，拒绝 bool | 控制每批源数据规模，不承诺各后端相同的文件数量和吞吐。 |
| `verify` | 默认 `None`；推荐 `"row-count"` | 未传时不做额外行数校验；不擅自改为默认开启。 |
| `dry_run` | 默认 `False` | `True` 只分析源与静态参数，不建目标、不连目标。 |
| `specs` | 默认 None，按已选源表名映射到 TableContract 或布局覆盖字典 | 保持源逻辑字段，允许调整目标 partition/segment/physical_layout；不传则沿用源布局。 |
| `temp_dir` | 默认 `None`，接受路径字符串；实现兼容 `os.PathLike` | 自动选择临时工作根目录；本地源默认 `源库/__iosql_migrations__/`，每次调用创建独立子目录。目标侧发布临时目录由库管理。 |
| `lock_timeout` | 默认 `30.0` 秒，非负数，拒绝 bool | 获取本次全部迁移锁的总等待期限；0 表示立即尝试。超时释放已拿到的锁，不代表整个转换必须在 30 秒内完成。普通读写遇迁移锁默认立即报忙。 |

旧入口接受的 `verify=False/"none"` 等兼容值继续归一化为 `None`，新文档只展示规范写法。

`mode="append"` 不自动跳过目标中已有的相同主键；冲突处理和主键约束范围遵循目标 `write()` 契约。`mode="replace"` 目前与 `upsert` 共用写入路径，仍可能有后端细节差异；它不等价于 `DROP TABLE` 或 `truncate()`。需要清空数据时必须另外显式调用已有的删除接口。

### 4.3 两个示例使用同一套流程

示例 11 与 16 的迁移主体统一为下面的结构。`SOURCE_TABLE`、`TARGET_TABLE` 和 URI 由各示例定义，省略 `batch_size=2` 这类用于演示分批的小参数。

```python
from iosql import Database

with Database(SOURCE_URI) as source:
    preview = source.migrate_to(
        TARGET_URI,
        tables={SOURCE_TABLE: TARGET_TABLE},
        verify="row-count",
        dry_run=True,
    )
    print("迁移预览:", preview.to_dict())

    result = source.migrate_to(
        TARGET_URI,
        tables={SOURCE_TABLE: TARGET_TABLE},
        verify="row-count",
    )
    print("迁移结果:", result.to_dict())

with Database(TARGET_URI) as target:
    rows = target.table(TARGET_TABLE).select(
        where={"field": "symbol", "op": "==", "value": "AAPL"},
        order_by="ts",
    )
    print("目标数据:", rows)
```

全库入门片段可以省略 `tables`；单表入门片段可以使用 `source.table(name).migrate_to()`。示例 11 未配置 PostgreSQL 时仍只预览；这种环境分支放在执行前，不改变迁移 API。示例准备阶段仅处理本示例专用资源，删除行为与迁移主体分开说明。

源表准备统一使用 `timeseries(time="ts", pk=["symbol", "ts"], ...)`。目标查询先使用各后端已有的通用 `select()`；Parquet 的深度校验、文件查看等操作可以放到后端能力补充部分。

### 4.4 PostgreSQL 作为源的现有边界

当前重开 PG 作为迁移源时，仍需绑定同一份契约：

```python
with Database(pg_uri) as source:
    source.table("pg_bars", spec=bars_spec)
    result = source.migrate_to(
        "sqlite://db/restored.iosql",
        tables={"pg_bars": "bars"},
        verify="row-count",
    )
```

`bars_spec` 是原表的 `spec()` 或显式保存的同一份契约。该步骤是当前后端限制。PG 契约持久化可以作为独立后续能力，不能在统一示例时假定已经存在；未绑定的 PG 源表在迁移前给出带修复指引的错误。

## 5. 统一行为契约

本节描述待实施的共同约束。现有的参数名和默认值保持，部分校验会从执行中或执行后提前到执行前。

### 5.1 数据范围与后端能力

- 将表数据、列类型、主键及契约中的业务语义和元数据传给目标；目标支持的逻辑分区按对应后端落地。PG 契约的持久化边界仍按第 4.4 节处理。
- 不承诺物理文件、segment 数量、SQL 二级索引、触发器、权限和数据库特有对象逐一复制。PG 没有本地文件 segment，不能伪装成文件布局完全一致。
- 目标不支持的契约或写入组合应明确报错。例如目录目标的 `Segment.rows` 不支持 `upsert/replace`，迁移不能静默改成 `append`。
- 当前源数据如果存在跨文件重复主键，目标的约束可能更严格；不能自动去重后声称完整复制成功。
- 空表正常创建目标契约，迁移批数为 0；`row-count` 验证时源、目标均为 0 可以通过。

### 5.2 先校验，再写入

Database、Table 和 CLI 共用以下顺序：

1. 归一化参数；校验目标协议、batch_size、mode、verify、临时位置和名称映射。
2. 校验源和最终目标身份。本地真实路径相同或互相包含时拒绝；只允许内部管理的临时目录位于源库内，不能放开用户最终目标的嵌套限制。
3. 先在源共享租约内验证源表选择与最终目标契约；`dry_run=True` 继续统计源行数后返回，不创建临时业务目录、目标锁或目标连接。预览发现待恢复源库只报告，不自动恢复。
4. 正式执行释放预检租约，再按固定顺序申请源/目标独占锁，并重新确定源表集合和契约以防间隙变化。预检及后续锁共用等待期限。维护窗口从取得独占锁开始，之后源和目标协作 API 均禁止新的读写。
5. 只读检查全部已选正式目标表的存在情况、结构、物理身份及必要行数；不能用自动建表的 `table(spec=...)` 代替检查。PG 正式源快照/目标连接持有各自选中关系的 ACCESS EXCLUSIVE 锁，不能覆盖本次读取的源表。
6. 在临时位置完成复制；已有本地目标需要合并时，先生成目标副本，在副本中应用 `mode`，保留未被选中的正式表。
7. 校验临时结果、完成持久化准备，在已有独占锁内按第 10 节的提交协议统一发布并确认，然后在 `finally` 中解锁；耗时清理放在解锁后。

对已选目标的检查覆盖整次操作。复制中的故障只影响临时数据；对协作 iosql 读者的整次操作可见性，需要第 10 节的库级锁、提交记录和恢复流程共同保证。

### 5.3 预览

`dry_run=True` 的含义在所有入口保持一致：

- 不建立目标连接，不创建目标目录或表，不加载仅用于目标执行的依赖，也不创建临时业务目录。源协调锁可首次创建；预览和其他读取同样遵守共享锁，不能绕过正在进行的迁移。
- 返回源表名、目标名、源行数和批次数估算。缺失的源表不能因读取预览而被自动创建。
- 不检查目标是否在线、账号是否有写权限、目标是否为空；预览成功只代表源与静态参数通过检查。
- 读取源行数可能扫描数据，预览不保证是常数时间操作。
- `rows_copied=0`，`target_rows=None`，`verified=None`；`batches` 保持现有的估算含义，正式结果中的同名字段表示实际成功批数。预览也给出 source_spec/target_spec，目标布局可直接检查。

### 5.4 行数校验

`verify="row-count"` 表达完整复制的行数核对：

1. 正式执行前，所有被选中的目标表必须不存在或为空；已有非空目标在写入任何选中表前报错。目标库可以有其他无关表。
2. 已存在的空表允许使用，但需要先验证与源契约兼容。Python 与 CLI 按同一规则处理。
3. 源和目标独占锁均取得后记录源行数；发布前检查 `source_rows == rows_copied == 临时目标行数`，通过后才允许发布。
4. 不相等时抛出带表名和计数的校验异常，不能返回成功结果。

行数相同不能证明每个字段的值相同。新的安全流程在整个迁移期间禁止协作读写，跨文件一致性由库级独占锁保证。直接绕过 iosql 的本地文件访问必须由调用方停止。PG 源的计数、锁和读取在同一受控事务连接中完成。

已有非空目标的合并沿用 `mode=`，不使用完整复制的 `row-count` 校验。这里不提供“目标行数增量等于源行数”的替代规则，`ignore/upsert` 下这个推导并不成立。

### 5.5 失败、重试与资源

- 临时位置中的批次可以分别提交；发布前失败不修改正式目标，保留临时数据和任务记录供检查。
- 发布中的崩溃按持久化提交记录恢复；恢复完成前拒绝正式读取。不能仅依赖进程退出自动释放文件锁。
- 不删除正式目标来模拟回滚，不在异常后继续迁移其余表。
- 发布已确认后重复调用仍遵循原有 `mode`，并非业务幂等。发布结果不确定时先按任务 ID 查清状态，不能盲目重新追加。
- 目标连接由迁移流程创建并在 `finally` 中释放；调用方传入或持有的源 Database/Table 由调用方管理。内部创建的迭代器提前失败时也必须关闭。
- 内存以批次为单位，数据复制部分约为 `O(batch_size × 行宽)`，另计引擎缓冲和元数据；不能调用 `select()` 或 `list(iter_rows())` 先加载全表。

## 6. 结果与错误接口

### 6.1 延续已有结果类型

Database 入口返回 `MigrationResult`，包含 `tables: list[TableMigrationResult]`；Table 入口返回 `TableMigrationResult`。预览使用 `dry_run` 区分；保留 `.to_dict()` 及已有字段。参数统一不要求改变现有返回类型，两个结果已有相同的核心计数、校验与 URI 字段。

| 字段 | 统一解释 |
| --- | --- |
| `rows_copied` | 已成功交给目标 `write()` 并正常返回的源行数，保留历史含义。`ignore` 时包含被跳过的源行，不等于目标新增行数。 |
| `batches` | 正式执行时是成功批数；预览时是按源行数估算的批数，物理分区边界可能使实际值不同。 |
| `source_rows` / `target_rows` | 实际计数或 `None`；未计数不能用 0 代替。 |
| `verified` | 未执行校验或仅预览为 `None`；通过为 `True`；失败表的异常结果中为 `False`。 |
| `tables` | Database 结果中的逐表结果，包含源名 `table` 和 `target_table`。 |

追加 `rows_written: int | None`，汇总目标 `WriteResult.rows_written`，帮助合并场景判断实际受影响行数：

- 放在 dataclass 末尾并给默认值 `None`，避免破坏已有位置构造；`to_dict()` 只追加新键。
- 任一成功批次的写入数未知时，整张表以及相应的整库汇总值为 `None`；预览也为 `None`。
- 正式执行的空表为 0；不能用 `rows_copied - rows_written` 推导所有后端的跳过数。
- 表示目标后端报告的写入或影响行数，仍不等价于目标表行数增量。

`rows_written` 是结果增强。安全发布另外追加 `published: bool | None = None` 和 `operation_id: str | None = None`：预览不发布、成功执行为 `True`，失败时按任务状态判断；旧构造器的 `None` 表示没有发布状态信息。入门仍只需了解 `.rows_copied`、`.verified`、`.published`、`.to_dict()`。

源数据的计数不包含已有目标副本的复制，也不包含跨文件系统的内部搬运；这些内部步骤不能重复累加 `rows_copied` 或 `rows_written`。

### 6.2 错误可判断，部分完成可检查

继续使用 `iosql.exception` 中的异常体系：

| 情况 | 异常与恢复方向 |
| --- | --- |
| 参数非法、源表不存在、缺契约、目标非空但要求完整复制校验 | `BadArgument`；指出参数或表名，提供修复示例。 |
| 已有目标契约不兼容 | `TableSpecConflict` / `SchemaConflict`；选择新目标或提供兼容结构。 |
| 源目标指向相同存储、目录嵌套等不安全关系 | 沿用现有 `BadArgument`，使原有调用方仍可捕获。 |
| 行数校验失败 | 新增 `MigrationVerificationError(MigrationError, BadArgument)`，保留已有捕获兼容性；不发布临时结果。 |
| 复制中的网络、存储或驱动执行失败 | 新增 `MigrationError(IosqlError)`；保留原始异常链，附上源表、目标表和失败批次。已有的 `IosqlError` 优先保留原类型并附加上下文。 |
| 普通读写遇到迁移独占锁 | 新增 `DatabaseBusy(IosqlError)`，立即提示库正在迁移，调用方稍后重试。 |
| 迁移等待库级独占锁超时 | 新增 `MigrationLockTimeout(MigrationError)`，给出当前阶段及任务 ID，释放已经取得的锁。 |
| 发布过程中断或提交状态尚未确认 | 新增 `MigrationRecoveryRequired(MigrationError)`，给出任务 ID 与恢复记录位置；恢复前不开放读取，不暗示一定已回滚。 |

执行阶段的异常追加可选 `partial_result`，记录临时位置已处理的表和批次，并明确 `published`。临时转换成功不表示正式目标已经更新，尚未转换的表也不表示成功。

临时失败批次可能部分落盘，`partial_result` 不构成可直接续传的检查点。提交失败后的恢复依据任务日志，不依据行数猜测。预检失败不需要制造“部分完成”的结果。

## 7. 内部接口与职责

建议将共同流程收敛到私有模块 `iosql/migration.py`，Database 和 Table 都调用该流程。业务使用者使用同一组迁移参数，继续使用现有结果对象。

| 层次 | 职责和契约 |
| --- | --- |
| `Database.migrate_to()` | 以当前库为可选源范围，将 `tables` 归一化为源→目标映射；构造一次任务，返回 `MigrationResult`。 |
| `Table.migrate_to()` | 以当前表为可选源范围；复用相同映射规则，拒绝其他源名；调用共同执行器，返回 `TableMigrationResult`。 |
| `Database.to_postgresql()` | 保留 PG URI 约束，委托 `Database.migrate_to()`，不另做复制或计数。 |
| CLI `migrate` | 将 `--table` 归一化后调用 `Database.migrate_to()`；仅负责解析、序列化和错误展示。CLI 源 URI 范围仍沿用现有约束。 |
| 共同迁移流程 | 参数归一化、身份校验、预览、库级独占、临时转换、校验、发布、恢复、异常上下文与资源释放。 |
| 后端 Table | 继续实现 `spec()`、`iter_batches()`、`count_rows()`、`write()`，负责具体存储行为。 |
| 目标只读检查 | 提供私有的“是否存在、物理身份、结构和必要行数”查询；检查本身不能创建表或写元数据。 |

核心数据类型保持少量私有概念：

- 每项迁移输入为源表对象、目标名和已确认的 `TableContract`；归一化后目标名不重复。
- 公共流程接受 `Sequence`，不会在迭代源数据时改变任务集合。
- 读接口类型为 `Iterator[list[dict[str, Any]]]`，每批最多 `batch_size` 行；写接口返回 `WriteResult`。
- 后端检查返回包含 `exists: bool`、物理身份、结构信息和 `row_count: int | None` 的私有结果，不返回仍占用连接的游标。

保留 `Database._new_migration_target(target_uri)` 作为可替换工厂，并为临时目标与发布器提供同等注入边界；不引入全局连接或隐藏的后台线程。单次调用按表、按批顺序执行，同一源或目标的并发迁移通过库级独占锁串行化。锁顺序和读取覆盖范围按第 10 节执行。

先进行全部前置检查，再进入写入阶段。源数据的读取顺序不构成公共排序承诺，迁移不为复制额外引入全局排序成本。

## 8. 方案比较与兼容性

| 方案 | 优点 | 代价 | 选择 |
| --- | --- | --- | --- |
| 每个目标一个方法，例如 `to_postgresql/to_parquet/to_sqlite` | 单个方法名容易猜测 | 新增后端持续扩张方法，双向迁移换动词，重复示例和规则 | 保留已有兼容方法，不继续扩展这一模式。 |
| Database/Table 都使用 `migrate_to(target_uri, tables=...)` | 名称、参数、映射含义相同；默认范围由对象确定 | Table 改名需要重复一次源名；旧参数需兼容归一化 | 推荐。 |
| 另按整库与指定表拆分方法名 | 方法名直接表达范围 | 不能解决用户希望 Database/Table 参数一致的问题，增加推荐名称 | 本轮不采用。 |
| 再引入 `copy_to()` 或顶层 `migrate(source, target)` | 可以重新设计更严格的默认值 | 与已有入口形成额外词汇和兼容层 | 本次没有足够收益。 |
| 预览对象再 `.execute()` | 可以封装大型迁移任务 | 增加生命周期和状态一致性要求；预览后源、目标仍可能变化 | 保留已有 `dry_run` 调用方式。 |

兼容要求：

1. 保留 `mode="append"`、`verify=None`、`dry_run=False`、`batch_size=10_000`，不擅自改变已有默认值。
2. `to_postgresql()` 继续可用，不新增强制废弃警告或删除时点。
3. 新代码在两个作用域都使用 `tables=`；返回类型不变。旧 Table 的 `target_table=`、第二位置参数作为目标名的调用继续兼容，先归一化为 `{当前源名: 目标名}`。旧 Database 的字符串、tuple、set 选表继续兼容。
4. 兼容层必须区分旧位置调用与新 `tables=` 关键字，不能把旧单表目标名误当成源表名。Table 同时显式传入 `tables` 与旧 `target_table` 时抛 `BadArgument`，不设隐式优先级；用内部未传标记区分省略与显式 `None`。兼容层可保留额外参数，统一签名描述推荐调用面，不宣称运行时已移除旧参数。
5. 新增结果字段只追加；`rows_copied` 不重新定义为新增行数。
6. 单表前置校验、目标映射碰撞检查、`row-count` 空目标检查的收敛需要在发布说明中写清楚；这是行为变更，不能混进示例格式修改而不说明。
7. `repartition_to()` 保持显式改变布局的职责及返回类型。可以逐步共用底层批次复制，但不因此改变它的公共参数和行为。
8. 用户新增的安全要求改变执行与可见性契约：从直接写正式目标改为临时转换后发布，且迁移全程禁止读写。这些行为必须在版本说明中明确，完成相关锁与恢复实施后才可声明支持；不能把它当作现有 v0.3 的能力。

## 9. 落地范围与完成标准

先完成表达统一：

- 示例 11、12、16 和全库片段统一使用 `migrate_to()`；Database/Table 改名都展示 `tables={源名: 目标名}`。后端差异由 URI 和环境准备表达。
- 11 与 16 使用相同源表建模、预览参数、结果打印及基础目标查询模式。
- README、示例索引、`skills/iosql-usage` 示例副本与参考指南同步；兼容方法只在兼容说明中展示。

再完成共同契约：

- 三后端的单表入口、Database 与 CLI 共用校验和执行流程。
- 将目标只读预检放在全部数据写入前，统一 `row-count` 对不存在、空、非空目标的处理。
- 默认在临时位置完成转换；将库级锁接入全部本地读取与写入入口，实现发布日志和恢复流程，再对外承诺不暴露中间状态。
- 追加准确的写入计数与失败上下文，保留既有异常捕获及结果字段兼容性。

后续实施需要覆盖以下验收场景；本次设计工作只进行了源码和文档静态复核，没有运行测试：

1. SQLite → Parquet、SQLite → PG、Parquet → SQLite、已绑定契约的 PG → SQLite 使用相同调用结构；未验证的后端组合不宣称已通过集成验证。
2. Database/Table 接受相同 `tables=None/列表/字典` 形式；`None` 分别指当前库与当前表。Table 选其他表、空选择、多源同目标在写入前失败；相同单表映射得到相同目标内容，两个返回类型保持兼容。
3. dry-run 对不可连接的目标可返回源预览，不创建目标资源；各入口均检查非法 URI、mode、batch_size 和本地自迁移。
4. `row-count` 对不存在和兼容空表成功，对任何已选非空目标在写其他表前失败；不拒绝无关目标表。
5. 目标能力不支持的写入组合、已有契约冲突在可判断的最早阶段报错。
6. `ignore` 场景区分源行数与实际写入计数，不能把未知数报告为已写入全部源行。
7. 注入第二张表或中间批次故障，正式目标不变、临时进度可检查，所有内部连接和流均释放。
8. 保持旧 `Database.migrate_to()`、`Table.migrate_to()` 和 `to_postgresql()` 的参数位置、默认值、结果类型及 `BadArgument` 校验异常捕获兼容。
9. 迁移开始前等待已有流结束；独占锁取得后，新读写立即报忙，直到转换和发布完成。流式读取持有完整共享租约，不会在同一查询中混用发布前后的目录。
10. 在每一步目录重命名、日志持久化及 PG 提交边界强杀进程，重开后只得到完整旧结果或完成恢复后的完整新结果；无法恢复时明确拒读。
11. 同文件系统与跨文件系统都在最终目标同文件系统内发布；临时管理目录不会混入源表枚举、备份或重分区任务。
12. 人为拖慢成功后的临时目录清理，新读写仍能在清理完成前拿锁；正常异常均不留下占用锁，进程退出后不能因泄露或继承锁描述符而继续占锁。

本设计复核重点：简单调用保持短小；只读预览没有目标副作用；PG 契约重开限制明确；`replace` 没有被解释为整表覆盖；失败结果没有被解释为自动续传能力。

## 10. 临时转换、锁与安全发布

本节根据用户“转换完成后再同步、临时目录默认在原始目录、迁移期间最好禁读写、文件锁解除要快”的补充要求制定，替代原先直接向正式目标逐批写入的执行方式。

### 10.1 用户调用与默认工作流

```python
with Database("sqlite://db/market.iosql") as source:
    result = source.migrate_to(
        "parquet://db/archive.iosql",
        verify="row-count",
    )
    assert result.published

# 需要时才指定临时位置和锁等待时间。
with Database(source_uri) as source:
    source.migrate_to(target_uri, temp_dir="/fast-disk/iosql-tmp", lock_timeout=60.0)
```

默认工作流：

```text
参数预检 → 独占源库和目标库 → 临时转换/合并 → 校验并落盘
                                                   ↓
                                提交发布 → 确认完成 → 解锁 → 清理临时文件
```

采用自动加锁的维护窗口方案：取得独占锁前，等待已有操作结束；取得后，源和目标均不接受新的读写。普通读写立即返回“库正在迁移”，不进入长时间等待。迁移完成并解锁后恢复正常操作，调用方无须手工管理临时目录和锁。

这是单次完整复制或合并的发布协议，不是持续同步服务。如果需要源库持续写入，必须增加能捕获插入、更新、删除及事务顺序的变更日志和末尾追赶阶段；不能仅按最大时间戳补数据。

### 10.2 临时目录默认位于原始本地库

本地源默认结构：

```text
db/market.iosql/
  bars/
  events/
  __iosql_migrations__/
    <operation-id>/
      owner.json       # 库标记、任务 ID、源/目标身份与路径
      progress.json    # 转换进度；不作为发布提交依据
      payload/         # 完整的临时目标库/选中表结果
```

- `temp_dir=None` 使用上述保留管理目录；传入路径则在指定目录内创建独立任务子目录，不能直接把用户指定目录当作可删除的任务目录。
- 任务目录使用不可预测的唯一 ID；管理标记与文件类型都验证后，才允许识别、恢复或清理。拒绝符号链接及越出所属任务的路径。
- 普通用户不能以 `__` 开头建表，现有代码依据为 `iosql/engines/sqlite/table.py:573`。该保留目录从 `table_names()`、全库迁移、布局诊断和普通备份的数据枚举中明确排除。
- `is_iosql_database()` 等严格身份检查需要认识带有效管理标记的该目录，不能简单忽略所有未知目录。备份不携带活跃任务及锁；恢复后重新建立协调状态。
- `dry_run` 不创建此目录。成功且提交记录落盘后可清理本任务临时数据；失败保留，禁止顺带清理源业务数据或其他任务。

PG 源没有原始本地目录：任务工作区默认使用系统临时目录的 iosql-migrations/<任务ID>/，可通过 temp_dir 覆盖。PG 目标的业务暂存数据使用目标数据库专用 schema，原始本地目录只保存协调信息，不要求把 PG 数据先导出成本地文件。

### 10.3 跨文件系统需要目标侧暂存

默认临时目录位于源库，并不能保证与目标在同一文件系统。跨文件系统时采用：

```text
源库/__iosql_migrations__/<id>/payload
    → 完整复制到目标逻辑目录/__iosql_generations__/<id>/
    → 校验、关闭连接、持久化
    → 在目标文件系统内执行发布
```

复制到目标侧暂存期间，正式目标仍保持旧状态，绝不直接边复制边覆盖正式路径。额外复制会增加空间与耗时；预览不触目标，只展示源和最终契约，实际位置按任务 ID 与目标控制记录追踪。

`os.replace()` 在跨文件系统时可能失败，且不能直接覆盖非空目录；成功的单次重命名才具有其文档描述的原子性。因此跨盘 `shutil.move()` 不能作为原子发布的替代。[Python os.replace 文档](https://docs.python.org/3/library/os.html#os.replace)

### 10.4 每个库使用同一把读写锁

每个本地库使用一把**由所有 iosql 入口遵守的库级跨进程读写锁**。普通读写获取共享锁，迁移获取独占锁；原有表级写锁继续负责普通并发写入的正确性。

| 操作 | 库级锁 | 持有范围 |
| --- | --- | --- |
| 普通读取、表名和元数据查询 | 共享锁；拿不到立即报忙 | 覆盖整个操作。 |
| 流式读取 | 共享锁 | 持有到耗尽或显式关闭。 |
| 普通写入、建表、改契约、compact、删除等 | 共享锁，再进入原有表级锁 | 覆盖整个操作及实际事务生命周期。 |
| 迁移 | 源和目标都独占 | 从计数前一直持有到发布确认或失败退出。 |
| 恢复、rename、restore 等改动存储身份的操作 | 独占涉及的库 | 覆盖恢复或目录切换全过程。 |

固定顺序是“库级锁 → 表级锁”。涉及多个库时按稳定存储身份排序取得库锁，避免 A→B 与 B→A 互相等待。获取全部锁共享一个 `lock_timeout` 截止时间，任一失败立即释放已经取得的锁；不先开始转换。

锁文件放在库父目录的稳定管理空间，如 `.iosql-locks/<库身份>.lock`，不能跟着被替换的目标目录移动。同一真实目录使用同一锁身份，不能因 URI 的后端前缀不同而绕开。持有中的锁文件不能删除后重建，避免不同进程锁到不同 inode。

同进程使用锁租约管理嵌套操作：迁移内部读取源、检查目标时复用已持有的独占租约，不额外申请共享锁或把独占锁降级。同线程已经持有读取流再发起迁移时明确拒绝，要求先关闭流。锁描述符不泄露、复制或传给子进程，进程内状态在 fork 后重新建立。

解锁前写入新的库 generation；每个缓存对象在下一次取得共享锁后检查 generation，先关闭旧连接、失效 manifest 缓存，再读取新目录。仅改名目录而继续复用旧连接不满足读取保证。

**快速解锁：** 提交与必要的持久化完成后，在 `finally` 中直接执行 `LOCK_UN` 并关闭锁描述符，不等待临时目录和旧副本的大量文件删除；清理失败只报告残留。普通读写使用非阻塞申请，锁释放后下一次调用即可重新申请，没有额外的“等锁文件过期”或固定解锁延时。内核文件锁由显式解锁或关闭全部所属描述符释放。[Linux flock 文档](https://man7.org/linux/man-pages/man2/flock.2.html)

保证范围是接入该协议的 iosql 同机进程。本地直接操作 SQLite/Parquet 文件的外部程序以及未接入协议的旧版 iosql 不受这些锁约束，切换前必须停用其访问。

### 10.5 本地发布与崩溃恢复

新目标与已有目标共同使用完整物理代际。工作区在源逻辑目录内；最终代际位于 `目标逻辑目录/__iosql_generations__/<operation-id>/`。所有未选中目标表、现有根文件也进入一致的目标副本；选中表在副本中应用 `mode`。暂存不能与正式目标通过硬链接共享。SQLite 每文件使用 backup API 合并已提交 WAL；不能只复制主文件。

```text
PREPARING → READY → COMMIT_DECIDED → COMMITTED
```

1. `PREPARING`：任务工作区记录身份；目标正式视图未改变。
2. `READY`：完整转换及实际计数、深度身份校验通过，关闭暂存连接。最终代际在目标文件系统上准备好，跨盘复制后再次校验，然后同步数据、目录及目标根。
3. 在稳定控制记录中持久化 `COMMIT_DECIDED`：包含目标相对物理路径、任务 ID、目录 device/inode。旧 physical_root 和 generation 仍作为当前视图；提交决定后的正常读写先恢复。
4. 校验新根身份及 marker，一次原子替换控制记录，将新 physical_root、generation、`COMMITTED` 共同发布并同步。物理路径从不复用，旧空闲连接关闭不会触及新代际的 WAL。
5. 释放源/目标锁后，仅删除本次退休的旧代际或旧逻辑根业务文件，再删除本任务工作区。清理不修改全局控制状态，不扫描删除后来任务的代际。清理失败报告残留，不能撤销提交。

提交决定前失败保留暂存供排查。决定后中断，下一次受保护操作按目录身份重放指针；恢复信息不完整则拒绝访问，不猜测或自动删库。任务 ID 与已发布 generation 一致；清理不再单独写全局 CLEANED 状态，避免覆盖新迁移。

进程崩溃会释放 OS 锁，**所有读写入口都须检查未完成的提交记录**：发现记录后释放共享锁，取得独占恢复锁并重新检查，完成恢复后再重试操作。恢复失败则抛出明确异常，不能因为锁已释放就读取或写入混合状态。dry-run 只报告需要恢复，不写恢复数据。

文件同步不能只覆盖数据文件，目录项及提交日志所在目录也需按顺序持久化；文件 `fsync` 本身不保证父目录项已落盘。[Linux fsync 文档](https://www.man7.org/linux/man-pages/man2/fsync.2.html)

这里的保证依靠锁、持久提交决定与原子路由指针，协作读者只见完整的发布前或发布后结果。多次独立查询跨越发布时间仍可能分别读到旧版与新版；单次查询/流不能读到混合状态。

### 10.6 PostgreSQL 使用暂存表与事务提交

PG 目标使用专用暂存 schema/表。正式事务在迁移开始时取得独占锁并持续持有；暂存通过独立连接完成，转换和校验完成后才在持锁事务中应用正式变更：

- 从正式迁移开始就对涉及的已有源/目标表按固定顺序获取 `ACCESS EXCLUSIVE` 锁，禁止其读写；目标发布事务应用所有选中表的数据和必要 DDL，校验通过后统一 `COMMIT`。保持已有关系身份，避免简单换名使视图、外键仍指向旧表。
- 全新表可在同一事务内发布暂存表，但必须同时处理分区子表、索引、约束和命名冲突；暂存 schema 的清理不能误删尚在使用的对象。
- 迁移全程属于维护窗口，不能承诺长事务或复制本身很快；“解锁快”表示完成后及时释放，不把临时清理算进锁持有时间。
- 任务提交状态与正式数据在同一目标事务中记录；`COMMIT` 回复丢失时用任务 ID 查清状态后再决定后续动作。
- PG 源的计数和流式读取必须复用持有源表独占锁的同一事务连接，否则迁移自身也会被锁住。目标正式变更同样使用持锁连接，暂存操作不触碰被其他内部连接独占的正式表。
- 另以库身份的 advisory lock 接入 iosql 的普通操作入口，保证所有使用该库的 iosql 读写统一报忙；服务器表锁覆盖直接 SQL 对所选关系的访问。

PG 的普通 `SELECT` 需要 `ACCESS SHARE`，`ACCESS EXCLUSIVE` 与之冲突；仅用 advisory lock 不能约束未参与该协议的 SQL 客户端。[PostgreSQL 显式锁文档](https://www.postgresql.org/docs/14/explicit-locking.html)

正式变更在一个事务里提交，用 PG 自身的事务可见性和回滚保证目标完整性；临时 schema 内的逐批提交不会让正式表出现半成品。[PostgreSQL 事务文档](https://www.postgresql.org/docs/14/tutorial-transactions.html)

### 10.7 完成这套保证的必要实施范围

需要同时修改迁移器、各读取入口、写入入口、流式租约、缓存失效、存储身份识别和恢复入口。仅增加临时目录或在迁移器中加锁均不足以达到上述保证。对应实现与回归由 PLAN-083 跟踪；有证据后逐项标记完成。

## 11. 审查补充与实施约束

实施补充：SQLite 连接池在操作结束后仍可能保留 WAL 连接。直接把旧目录移走、把新目录放到相同文件名，会使后续旧连接关闭时与新文件路径发生冲突。因此本地采用以下代际发布方案，第 10.5 节已同步采用此方案；锁、公共接口、临时目录选择和快解锁要求保持。

### 11.0 本地使用不可复用的物理代际路径

- 每次发布生成完整的新目标视图，路径为 `目标逻辑目录/__iosql_generations__/<operation-id>/`。已有目标的未选中表也进入一致的新视图；选中表在该视图中按原 mode 合并。
- 逻辑 URI 不变。协调记录原子保存 `physical_root`、generation 与提交状态；Database/Table 的路径解析、表枚举和身份检查都通过这一记录选择已发布视图。
- 旧物理文件在发布时不改名到新文件的路径，新代际路径永不复用。空闲旧 SQLite 连接即使尚未关闭，其 WAL/SHM 路径也不会与新代际重合。
- 完成新视图、校验与持久化后，先记录提交决定，再一次原子替换控制记录公布新物理根。崩溃恢复检查新根身份后重放指针发布，不逐表猜测目录移动状态。
- 原来位于逻辑根下的旧业务表及旧代际可在解锁后退休清理，但绝不能删除逻辑根或当前代际。清理仅针对任务清单记录的旧文件，不能扫描并删除后来任务的数据。
- 默认转换工作目录仍位于源逻辑库的 `__iosql_migrations__`。最终代际准备在目标文件系统内，跨盘先完整复制。
- 该物理路径解析必须同步覆盖现有重命名、备份/恢复及 CLI 维护入口，避免这些入口把管理目录误当作业务表或遗漏当前数据。

### 11.1 公共操作屏障

- Database 的取表、枚举、建模及 Table 的读写、维护、元数据、查询对象消费都进入同一协调入口。读取流持有共享租约直到关闭；同线程持有流时升级为迁移独占锁直接拒绝。
- 取得共享锁后先读取控制状态，待恢复检查必须先于缓存、manifest、业务文件和普通写入。`COMMIT_DECIDED` 时转独占恢复；不能只在查询时检查。
- 构造对象时的隐式 manifest 访问也受保护。迁移内部通过同线程租约复用独占权限，不将独占锁降为共享锁。
- 普通写入继续使用表级修改锁；库级共享锁仅阻止维护窗口，不改变日常同表写入的串行规则。

### 11.2 本地控制记录与独立恢复数据

- 每个本地库在父目录协调空间中保存稳定锁文件和单一 `state.json`，后者同时包含当前 physical_root、generation 及当前发布事务。原子替换文件后同步其目录。
- 转换工作目录仍默认位于源库。发布前将完整 payload 搬入目标逻辑目录下的代际管理空间：同文件系统用改名，跨文件系统先复制、校验并同步。提交后的恢复数据不再依赖源目录，避免源库后续重命名影响目标恢复。
- 提交决定记录新代际的 device/inode、相对路径及任务 ID，代际 marker 再次确认归属；恢复只重放指针，身份不符合即拒绝。旧物理路径从不被移到新物理路径。
- 完成发布只做一次原子控制记录更新，将 generation 更新与 `COMMITTED` 一起发布。清理只操作本任务已退休目录，不改写全局控制记录。
- 对不存在或内容不合法的源/目标目录、越界临时路径、符号链接和不属于本任务的数据提前报错。迁移目标的空表必须持久化契约，不能成为关闭后无法识别的空壳。源的既有延迟建表行为保留；当前实例已声明但未首写的表纳入迁移，重新打开旧空壳后仍须先声明契约。

### 11.3 PostgreSQL 的事务所有权

- 源快照、正式目标发布都使用迁移专用连接；调用方已有 Table 连接和未提交事务不被迁移提交、回滚或关闭。有调用方未提交事务时明确拒绝，提示先完成原事务。
- 私有源适配器的 count 与命名游标读取复用持锁事务；游标耗尽、关闭或异常只释放游标，不自主结束迁移事务。正式发布器同理，所有表完成及校验后由迁移器统一提交。
- 现有公共 `write(commit=True)` 和自动管理事务的流式查询不能直接充当正式迁移事务接口。暂存库的批次可以独立提交，源与正式目标事务不可以。
- 整库 advisory 锁由专用协调会话持有，业务表的 `ACCESS EXCLUSIVE` 由专用事务连接持有；两类生命周期都必须在 finally 中释放，不能等调用方关闭 Database 才释放。
- 正式数据、任务已提交状态及目标 generation 在同一 PostgreSQL 事务里修改。独立持久控制记录标识进行中的任务；提交应答丢失时，先等待原事务的任务锁结束，再查询任务状态，不能因为暂时看不到提交记录就判断失败。
- 提交状态保存在稳定的管理 schema 中，不随暂存 schema 清理而删除。PG 正式目标行数在发布事务里再次核对，不能仅凭暂存表的行数设置 verified。

### 11.4 接口兼容与验证

- 推荐 `tables=` 只使用 None、列表和映射。Table 仍兼容旧目标名位置参数和 `target_table=`，但两种参数混用明确报错；兼容归一化完成后再做作用域校验。
- 测试至少覆盖：输入归一化、源/目标忙、同线程流升级、跨进程锁释放、缓存失效、两个以上表的原子可见性、每个发布阶段进程强杀、待恢复写入、dry-run 不连目标、跨文件系统准备、PG 原事务隔离及最终事务统一提交。
- 实施按计划推进；设计中列出的安全能力只有实现及验证后才标为完成。

## 12. 迁移时调整目标分区（2026-10-18 补充）

这是常用迁移场景，直接由 `migrate_to` 支持。两个作用域都增加同一个关键字参数 `specs`，按**源表名**指定目标契约，不受 `tables` 改名影响。省略的表沿用源契约。字典值可以是完整 `TableContract`，也可以是只包含布局字段的覆盖字典；覆盖先合并源契约，再统一校验。

```python
# 按天改为按月，保留源列、主键和时间单位。
bars.migrate_to(
    target_uri,
    specs={"bars": {"partition": "calendar(date,every=1month,timezone=Asia/Shanghai)"}},
    verify="row-count",
)

# 选表、改名、修改分区可同时使用；键始终是源表名。
source.migrate_to(
    target_uri,
    tables={"bars": "archived_bars", "events": "events"},
    specs={"bars": {"partition": "duration(ts,every=1hour)"}},
)

# 去掉分区，或使用已有契约的 with_partition()。
bars.migrate_to(target_uri, specs={"bars": {"partition": None}})
bars.migrate_to(target_uri, specs={"bars": bars.spec().with_partition(every="1hour")})
```

- 支持修改 `partition`、`segment`、`physical_layout`、`force_partition`；添加、移除分区，修改分区列/间隔/种类均需满足现有列和时间契约。未选中表名、拼错配置键在暂存前报错。
- 不在此接口中执行任意列类型转换、删列、改主键、改变时间单位或时间含义。完整契约中这些逻辑字段必须与源一致，避免静默改值或丢行。
- 源契约、源文件不变；按源布局流式读取、按目标布局重新路由写入暂存，不能原样复制源 manifest 冒充重新分区。
- 已存在的目标表必须兼容**最终目标契约**；分区不兼容时在正式发布前失败，并提示使用新库或新表。`replace` 仍是主键冲突策略，不能据此清空既有目标或偷偷重建表。
- `dry_run` 返回 `source_spec` 和 `target_spec`，可以检查布局变化；继续只读源库，不连接目标。正常结果也保留这两项和 `partitions_created`。
- 重新分区仍经过整批暂存、实际行数校验、统一发布及恢复屏障，失败时不得暴露一半新布局。`repartition_to` 保留为兼容入口并复用此流程。

### 12.1 维护入口适配与资源成本

完整目标视图需要额外磁盘空间，包括未选中表的副本。备份使用只含当前已发布数据的独立快照，合并 SQLite WAL，不复制控制记录和退休代际；恢复通过新代际指针发布。已有代际的 Database.rename 采用快照发布后退休旧逻辑目录，不再承诺改名为常数时间。CLI repartition --replace-source 保持源独占锁直到新快照发布，备份支持跨文件系统。普通原始目录复制不能替代这些入口。

PG 的服务器 advisory 锁以实际数据库为作用域；协调资源归一化忽略账号、密码及 search_path，主机别名之间仍由同一服务器锁互斥。源/目标用不同主机别名指向同一个实际 PG 库时可能超时，不支持借别名进行同库迁移；使用一致 URI 和不同目标库。

### 12.2 数值时间与结果准确性

数值时间列转 calendar 时，运行时与 PG 数值分区边界共同使用原时间单位 ms/s；新增 manifest 字段只用于明确数值单位，旧日期布局保持兼容。不依赖 Arrow 猜测早期 epoch 的数量级，不通过改写源值迁就分区。迁移结果的 partitions_created 仅记录本次新增目标分区；兼容的 RepartitionResult 保持目标全部分区列表。batch_size 同时约束转换和 PG 正式写入批次。嵌套 CLI 维护中的清理也推迟到最外层库租约释放后。

### 12.3 已绑定的 PG 对象在发布后继续使用

Database 保留 PG 实例缓存；旧 Table 在下一次受保护操作中关闭旧连接，只读验证关系身份和结构，按同 OID 的本次持久目标契约或原绑定恢复属性。选中及未选中表不因全库 generation 改变丢失绑定，读取不会自动创建已删除关系。新打开的 PG Table 仍遵守原有显式绑定约定。

### 12.4 CLI 直接文件访问与恢复授权

普通 source 命令的共享租约覆盖完整命令，包括 index/doctor 直接扫描物理文件的间隙。migrate/repartition/backup/restore/rename 使用各自独占边界；web 启动命令不把共享租约延长到整个服务器寿命，业务调用仍由库入口协调。restore 在源/目标独占范围重新检查目标存在与 --replace --yes，防止加锁前目标被其他协作进程创建后遭无授权覆盖。

### 12.5 状态历史与持久化目录项

当前控制记录保持常数大小。覆盖已 COMMITTED 的本地当前记录前，先在 `.iosql-locks/<库token>.migrations/<operation-id>.json` 持久归档，后续清理不删除历史状态；当前提交任务在 state.json，旧提交在归档。错误提供 recovery_path，避免应答不确定后因后续发布失去查询依据。新建协调目录、归档目录、目标根及备份根的父目录项也需 fsync，不能只同步内部文件。

### 12.6 缓冲写入

已有 buffered_writer 的 append/flush 同样受库租约约束；with 上下文持续持共享租约，迁移须等其 flush/退出。flush 失败保留输入，不自动重试。没有使用上下文的缓冲写入者应先显式 flush；迁移复制已持久化源数据，不把内存中的待写缓冲视为源库记录。URI 中显式 connect_timeout 在专用 PG 连接上保留，不被锁等待配置覆盖。

## 13. 审查修订（2026-10-18 15:39）

基准 abc30fee0d812626d140bb3d9fcf7a870ac953e7；本节由 Codex（GPT-6，精确模型 ID 未提供）修订，实施见 docs/dev/PLAN-084-model-unknown-migration-review-fixes.md。

- 发布后仍持独占租约时，将旧物理内容退休到库父目录稳定协调空间内的任务专属目录；锁外删除不再解析旧逻辑路径。若退休失败，仅报告残留，不删除可能已经复用的路径、不撤销新视图。
- 快照过滤必须区分业务目录与 SQLite 侧车文件；合法业务表名不得因以 -wal/-shm 结尾或名为 .iosql-locks 而被忽略。
- Windows 使用原生共享/独占文件锁，保持与 flock 相同的协调语义。普通访问不因缺少 fcntl 或 POSIX 目录同步接口失败；Windows 不宣称与 POSIX 目录 fsync 相同的断电持久性，原机及断电测试另行记录。
- allow_empty=True 可以接纳全部为合法空表 scaffold 的目录库，但不接纳无关子目录；未写入的本地表声明跨代际刷新仍保留，新视图已有持久契约时以新契约为准。
- 内部关闭和重连不得把有效句柄永久标记为用户关闭；calendar 持久分区范围保留既有毫秒格式，仅在分区裁剪比较时把秒级条件换算为毫秒，segment 的实际值统计仍使用秒。不要求重写已生成的 manifest。
- ui 与 web 别名的启动行为一致，均不占用服务器整个生命周期的库共享租约。

Windows 的共享/独占、立即返回和显式解锁参数依据 [Microsoft LockFileEx 文档](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)，通过标准库 ctypes 惰性绑定，不增加运行依赖。

PLAN-085 补充：Windows 打开最终 `.lock` 文件时使用 `CreateFileW(OPEN_ALWAYS, FILE_FLAG_OPEN_REPARSE_POINT)`，再通过 `GetFileInformationByHandleEx(FileAttributeTagInfo)` 检查已打开句柄，发现任何重解析属性均抛 `UnsafeOperation`。句柄仅共享读写、不共享删除，防止持锁期间替换锁文件；验证通过后转为不可继承的 CRT 描述符，验证或转换失败时释放原生句柄。依据 [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) 与 [GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)。原机验证边界见 `docs/dev/PLAN-085-windows-lock-reparse-points-OUTCOME.md`。

PLAN-086 补充：`resource()` 和 `physical_path()` 仅对锁身份使用 `normcase()`，文件系统访问、库控制目录和代际定位使用保留实际大小写的 `realpath()`。快照复制先对根目录和每个子项执行 `lstat()`，在过滤或递归前拒绝符号链接及 Windows 的 `FILE_ATTRIBUTE_REPARSE_POINT`，包含不会被 `is_symlink()` 识别的目录 junction；普通文件/目录分类复用该次属性结果。相关测试包含 Windows 原机的大小写敏感目录和 `mklink /J` 场景，当前 Linux 验证结果和原机执行边界见 `docs/dev/PLAN-086-windows-paths-and-snapshot-junctions-OUTCOME.md`。
