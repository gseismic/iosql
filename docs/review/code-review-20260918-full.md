# iosql 全量代码 Review 报告（2026-09-18）

- 日期：2026-09-18
- 代码基线：`0da5b0c`（实施 PLAN-061: 行数统计零 I/O 化）；当前 HEAD `0c172e2` 只新增了本报告初稿，未改产品代码。
- 范围：全部已提交代码，约 29,000 行 Python（核心库约 14,300 行、测试约 10,100 行）。核心 API、查询层、分区层、SQLite 引擎、PostgreSQL 引擎、CLI、UI（PLAN-055/059/060/061 新增）、utils/mysqlite、tests、examples及发布产物。
- 基线验证：`python -m pytest tests/ -q` → **406 passed, 16 skipped, 68 warnings**（本机 509.80s），通过数量与 PLAN-061 OUTCOME 一致；耗时主要来自默认纳入的 20 万行分区集成测试。
- 方法：初稿按 SQLite 引擎 / spec+partition+database / PG+core.query / CLI / UI / mysqlite+utils 六个领域通读；本次又对最高危结论做了独立最小复现，并补做 wheel 隔离安装、lint、类型检查和编译检查。下文标 **✅复核** 的条目有本机运行证据，其余明确标注原初稿实测或静态推断；报告不依赖易失的 `/tmp` 复现文件。
- 与上一轮的关系：`docs/review/code-review-20260823-v030-full.md`（PLAN-056/057 已修复 P0-1~P0-9）中仍有多项未修复，另见第四节「旧报告遗留项」。

---

## 一、总体结论

核心设计（manifest + 二级裁剪 + obsolete/generation 生命周期 + 只读 UI 隔离）依然稳固；上一轮 P0（写×compact 串行化、mysqlite IntegrityError 回滚、各注入点）确认已修。但本轮发现 **11 个新的 P0 级问题**，集中在四处：

1. **并发路径没有全部纳入串行化**：`delete()`/`truncate()` 与 `compact()` 不互斥，且读写共享 `auto_commit` 标志，导致删除静默丢失或行被 compact 复活（SQLite 引擎）。
2. **破坏性 CLI 防护形同虚设**：`_is_iosql_like_database` 判定的 `<db>/meta.sqlite3` 在真实库中不存在（正常库被拒），但任何伪造该文件的目录都能通过（可删任意目录）；`restore` 未校验 backup/target 重叠可自毁数据；`rename-database --overwrite` 裸奔。
3. **“预览/默认参数”承诺与实际行为不符**：UI 重分区 `dry_run:true` 实际写库；`select(symbols=/start=)` 静默忽略返回全量；`Partition.calendar(every=...)` 静默退化为 1day；mysqlite `insert_all` 把 SQL 表达式默认值当字符串写入。
4. **发布包不可完整使用**：源码树中的 UI 正常，但 wheel 未包含 `iosql/ui/static/**`；从 wheel 隔离安装后访问首页直接抛 `StaticFiles directory ... does not exist`。

**发布判定：NO-GO。** 在 P0-1/P0-2（数据一致性）、P0-3（路径越界）、P0-4/P0-5（恢复自毁）、P0-7/P0-8（PostgreSQL 删除/DDL）以及 P0-10/P0-11（UI 行为与发布产物）关闭前，不建议发布新版本或把写模式 UI 用于重要数据。

值得保留并继续演进的基础包括：显式 `TableContract`/结果对象、逻辑分区 + 物理 segment 的二级裁剪、obsolete generation 生命周期、默认 dry-run 的清理流程、只读 UI 的路由级隔离，以及覆盖 SQLite 主路径的较完整回归集。问题主要出现在跨模块边界、并发交错、破坏性命令边界和“源码测试 → 发布产物”的最后一公里。

### 问题统计（本轮合并去重后）

| 模块 | P0 | P1 | P2 |
|------|----|----|----|
| SQLite 引擎 | 2 | 6 | 13 |
| 核心元数据（spec/partition/database/table） | 1 | 7 | 11 |
| UI | 1 | 5 | 10 |
| CLI | 3 | 5 | 10 |
| PostgreSQL 引擎 + 查询层 | 2 | 4 | 13 |
| utils/mysqlite + utils | 1 | 6 | 9 |
| 发布工程 | 1 | 0 | 4 |

---

## 二、P0：数据正确性与安全（建议立即修复）

### P0-1 SQLite：并发读会把 delete/truncate 的删除静默回滚 **✅复核**
- 位置：`iosql/engines/sqlite/table.py:4061`（`_acquire_part_db` 改写共享 `MyDatabase.auto_commit`）、`:3325`（delete 按 `auto_commit=True` 取 part db）、`:3345`（`execute_params` 不传 `commit`，执行时读取 `db.auto_commit`）、`iosql/utils/mysqlite/connection_pool.py:206-209`（归还前无条件 `rollback()`）
- 机制：任意读操作（select/count/validate）用 `_part_db(..., auto_commit=False)`（table.py:930/954/2056/2077/2103/2136/2341/3070/3141/3175）会把该 part 文件共享 `MyDatabase` 的 `auto_commit` 翻成 False；delete 在进入上下文后、真正 DELETE 前若被读方改写，则 `execute_params` 不提交，连接归还时又被 rollback → DELETE 丢失，但 `delete()` 仍返回删除行数。
- 证据：30k 行压力探针曾连续 3 次复现；本次又用事件插桩固定交错顺序复现：删除前 `auto_commit=True`，并发读后变成 `False`，`delete()` 返回 1，但随后查询仍为 `[1, 2]`。
- 修复：① delete/truncate 显式 `execute_params(..., commit=True)`（最小修，仍建议根治）；② 根治：`auto_commit` 不应挂在共享对象上，应作为每次 checkout 的参数传入（`get_connection(auto_commit=...)` 或 per-lease 包装）。

### P0-2 SQLite：compact 与 delete/truncate 未互斥，已删行被“复活” **✅复核**
- 位置：`table.py:796`（compact 持 `_compact_lock`）、`:3273`/`:3302`（delete/truncate 不取锁）、`:3316`（`_delete_and_cases`）
- 证据：用事件在 compact 读完旧 segment 后暂停，执行 `delete(id=1)` 得到 `[0, 2, 3]`，恢复 compact 后查询重新变成 `[0, 1, 2, 3]`。
- 附带：delete 的 `_refresh_segment_info` 与写路径 `_increment_segment_info` 都是无锁 manifest 读-改-写，行数/stats 可互相覆盖（validate 报 row_count_mismatch）。
- 修复：delete/truncate/create_index/drop_index/manifest 刷新统一纳入 `_compact_lock`（或 per-table 路径级 RW 锁）。

### P0-3 核心：calendar `fmt` 未校验，分区文件可写出库目录（路径穿越） **✅复核**
- 位置：`iosql/spec.py:172-182`（`fmt` 原样接受）、`iosql/partition/calendar.py:167-169`（`format(self.fmt)` 生成 key）、`iosql/engines/sqlite/table.py:1897-1906`（`os.path.join(..., f"{part_key}.sqlite3")` 无 containment）
- 证据（本轮）：`partition='calendar(d,every=1day,fmt=../../../pwn)'` 写入后文件落在库根之外的 `pwn.sqlite3`；`fmt='YYYY/MM/DD'` 会创建多级子目录。写入路径无 `_is_safe_part_path` 校验（仅 cleanup/delete/query 有）。
- 影响：CLI `--partition`、Python API、被污染的 manifest 均可触发任意 `.sqlite3` 写入。
- 修复：`CalendarPartition` 收口 `fmt` 白名单（`FMT_SHIFT_MAP` 键），并禁止路径分隔符/`..`；`_get_part_dbfile` 增加 containment 断言（`UnsafeOperation`）。

### P0-4 CLI：`restore --replace` 防护失效——真实库被误拒 + 伪造标记可删任意目录 **✅复核**
- 位置：`iosql/cli.py:85-86`（`_is_iosql_like_database` 检查 `<db>/meta.sqlite3`）、`:1563`（restore 覆盖前调用）
- 证据（本轮）：
  - 真实 iosql 库只生成 `<db>/<table>/meta.sqlite3`，无库级 `meta.sqlite3` → `restore --target 真实库 --replace --yes` 必然报「目标目录不像 iosql 库」拒绝（功能不可用）；
  - `mkdir fake && touch fake/meta.sqlite3` 后 restore 成功，`fake/important.txt` 被删除并覆盖为备份内容（exit 0）——防护可被一个空文件伪造，等于可覆盖删除任意目录。
- 修复：改用可靠身份判定（存在 `*/meta.sqlite3` 表级文件或库结构校验 + `Database.table_names()` 可读），显式拒绝根/家目录/符号链接目标；删除前先改名到临时目录保证可回滚。

### P0-5 CLI：`restore` 的 backup 与 target 重叠时自毁数据 **✅复核**
- 位置：`iosql/cli.py:1555-1574`（先 `_rmtree_robust(tmp)` 再 `copytree`）
- 证据：`restore --backup db --target db --replace --yes` 的最小复现退出码为 1、异常为 `FileNotFoundError`，命令结束后目标目录和其中的 `important.txt` 均已不存在；`--backup bak --target 父目录` 同理会把备份连同目标一起删掉。
- 修复：resolve 后双向包含校验（backup==target、互为父子全部拒绝）；改为「copytree 到 target_tmp → `os.replace` 换入 → 删旧目录」，任一阶段失败可回滚。

### P0-6 CLI/core：`rename-database --overwrite --yes` 可删除任意目录 **✅复核**
- 位置：`iosql/cli.py:877-897` → `iosql/database.py:373-380`（`shutil.rmtree(target)`）
- 证据：对含 `important.txt` 的普通目录执行 `Database.rename(..., overwrite=True)` 后，源目录消失、目标变成 iosql 库且 `important.txt` 被删除；CLI 的 `--overwrite --yes` 直达同一路径。`Database.rename` 只有上一轮补入的双向包含检查，没有目标类型防护，也不拒绝 `/`、`~`（源库不在其内时）。
- 修复：`Database.rename` 的 overwrite 分支加目标身份校验 + 根/家目录拒绝 + 先改名到 tmp 再删（可回滚）。

### P0-7 PostgreSQL：`delete()` 对 falsy 非 None 的 where 静默全表删除 **✅复核（SQL 构造）**
- 位置：`iosql/engines/postgresql/table.py:1015`（`if where:`）
- 证据：用记录型假 cursor 调用 `delete([])`/`delete({})`/`delete("")`/`delete(0)`/`delete(False)`，五次均生成只有 `DELETE FROM Identifier('ticks')`、参数 `[]` 的 Composable，没有 WHERE；SQLite 对照为删 0 行 / 报错 / `BadArgument`。
- 修复：`where is None` 走显式 `delete_all`（加 DeprecationWarning），其余一律经 `Where` 校验，空的 list/dict/str 抛 `BadArgument`。

### P0-8 PostgreSQL：契约列类型原样拼进 DDL，可多语句注入 **✅复核（SQL 构造）**
- 位置：`iosql/engines/postgresql/table.py:228-233`、`:251-270`（未知类型 `value.upper()` 直通）
- 证据：`_pg_column_type('text); DROP TABLE users; --')` 原样返回 `TEXT); DROP TABLE USERS; --`，随后被 `sql.SQL(...)` 作为 SQL 片段而非参数/标识符拼入 CREATE TABLE。可达路径：`columns=`/`spec=`（TableContract 不校验类型值）；真实多语句执行后果仍需 live PostgreSQL 最终确认。
- 修复：类型走封闭白名单映射（补 `blob→BYTEA`、`datetime→TIMESTAMP`），未知类型抛 `BadArgument`。

### P0-9 mysqlite：`insert_all` 把 SQL 表达式默认值当字面量写盘 **✅复核**
- 位置：`iosql/utils/mysqlite/table.py:475`（读取 `dflt_value`）+ `sql.py:83,111-123`（Python 侧回填）+ `table.py:815-819`
- 证据（本轮）：列 `created_at TEXT DEFAULT CURRENT_TIMESTAMP`，`insert_all([...])` 落盘值就是字符串 `'CURRENT_TIMESTAMP'`（同表单行 `insert` 在原生 SQL 路径会正确求值）。`(datetime('now'))` 等同理。
- 修复：批量写入按行 key 集合分组（缺列省略，让 SQLite 求值），或识别非字面量默认值（`CURRENT_*`/括号表达式）跳回填并保证省略列。

### P0-10 UI：Maintain 的“预览（dry_run）”实际执行真实重分区 **✅复核**
- 位置：`iosql/ui/app.py:170-172`（`RepartitionBody` 无 `dry_run` 字段，pydantic 默认忽略未知字段）、`iosql/ui/static/js/tab-maintain.js:306-330`（`bodyOf(true)` 传 `dry_run:true`）、`app.py:1181`（直接 `repartition_to` 写库）
- 证据：write 模式 POST `/repartition` 带 `dry_run:true` → 200，目标库被真实创建并写入全部行。设计文档也明确没有服务端 dry-run。
- 修复：要么去掉该按钮/改文案为「写入新库（不可预览）」，要么实现只读估算；写模型统一 `extra="forbid"` 避免静默吞字段。

### P0-11 发布：wheel 漏掉 UI 静态资源，安装版 `iosql ui` 首页不可用 **✅复核**
- 位置：`pyproject.toml:33-41`（只有 `include-package-data=true`，没有 package-data/MANIFEST 配置）、`iosql/ui/app.py:1232-1237`（运行时依赖 `iosql/ui/static`）
- 证据：`python -m pip wheel . --no-deps` 构建成功，但 wheel 的 51 个成员中没有任何 `iosql/ui/static/**`；隔离安装该 wheel 后 `create_app()` 因 `check_dir=False` 能创建，实际 `GET /` 抛 `RuntimeError: StaticFiles directory '.../iosql/ui/static' does not exist`。
- 影响：源码开发环境和当前后端测试均为绿色，但 PyPI/wheel 用户无法使用项目已公开宣传的 UI 功能，属于发布阻断项。
- 修复：在 `pyproject.toml` 显式声明 `iosql.ui` 的 `static/*`、`static/js/*`、`static/vendor/*` 包数据；CI 增加“构建 wheel → 干净目录隔离安装 → GET `/` 并校验 JS/CSS 资源”的产物级冒烟测试。

---

## 三、P1：正确性与一致性（建议近期修复）

### SQLite 引擎

| # | 位置 | 问题 | 验证 |
|---|------|------|------|
| S1-1 | `table.py:431-448`、`:462-466` | `table.partition = ...` setter 只改 `meta["partition"]` 不同步契约：已有数据表改分区后等值查询静默漏数据、write 永久 `TableSpecConflict` | 初稿实测 |
| S1-2 | `table.py:2710-2711`、`:2656-2666` | `select()/iter_select*` 静默忽略 `symbols/start/end/date/session` kwargs，直接返回未过滤全集（与 `query()` 行为分裂，用户极易误用） | ✅复核 |
| S1-3 | `table.py:3053-3075` vs `:3142-3148` | segment 文件缺失时 `select()` 抛 `NoSuchTable`、`iter_select_batches()` 静默少数据；两条路径都会隐式重建空文件 | 初稿实测 |
| S1-4 | `table.py:1429-1470` | `validate(deep=True)` 用 `with sqlite3.connect(...)`（只事务不关闭）泄漏 FD；100 分区实测 22→42，Windows 下阻塞 `cleanup_obsolete` 的 `os.remove`（旧报告 S-M1 未修） | ✅复核 |
| S1-5 | `table.py:2767-2773`、`_project_fields:2831` | `select(fields=[...], to_dict=False)` 在需要额外排序字段时返回 dict 而非 tuple，违背冻结契约 | 初稿实测 |
| S1-6 | `table.py:450-466` | `db.table(name, spec=...)` 对已有表只存 `_declared_spec`、`spec()` 仍返回存储契约：`conflict/schema_policy/symbol_column/session_column` 等绑定被静默忽略（例：`conflict="ignore"` 仍抛 UNIQUE 错） | 初稿实测 |

### 核心元数据（spec/partition/database）

| # | 位置 | 问题 | 验证 |
|---|------|------|------|
| C1-1 | `partition/base.py:32-33` | `Partition.calendar(every='1week'/'4day'...)` 计算了 fmt 却**不传 every**，静默退化为 1day；README/SKILL 推荐的用法全部错分（PG native/CLI 同样受影响） | ✅复核 |
| C1-2 | `partition/range.py:18-20`、`duration.py:130-140`、`sqlite/table.py:1911-1912` | 浮点分区键生成 `0.0` 文件名，`select(where value=0)` 静默返回空（`0.0 != 0` token），duration 小数时间戳同理 | ✅复核 |
| C1-3 | `database.py:455-471`、`:110-126` | `migrate_to(dry_run=True)` 不纯：为不存在的源表创建目录与 `meta.sqlite3`（文档承诺不落盘） | 初稿实测 |
| C1-4 | `database.py:346-393` | `Database.rename` 不校验源/目标是否 iosql 库，可搬走/删除普通目录；symlink 目标抛裸 `OSError`、`EXDEV` 未包装 | 初稿实测 |
| C1-5 | `spec.py:219-244` + `duration.py:22-59` | `_format_duration_every(10_368_000_000)=='4month'` 但 `parse_interval_ms('4month')` 抛 `PartitionSpecError`：doctor/CLI 给出的重分区建议无法回读（月/年倍数词表缺失） | ✅复核 |
| C1-6 | `spec.py:309-322` | `TimeColumn(unit='date').normalize_value` 忽略 timezone：UTC 晚 8 点值在 Asia/Shanghai 下归一成前一天，date/start/end 过滤与裁剪错一天 | 初稿实测 |
| C1-7 | `database.py:152-169`、`sqlite/table.py:468-489` | `spec=` 路径静默忽略 `conflict/schema_policy`；`_ensure_spec_compatible` 不比较 symbol/session/order_key；`force_partition` 快捷路径丢失导致对已有表不可达 | 初稿实测 |

### CLI

| # | 位置 | 问题 | 验证 |
|---|------|------|------|
| L1-1 | `cli.py:1119-1135`、`database.py:451-537` | `migrate -s DB --target DB`（同源同目标）无守卫，静默把行数翻倍（`repartition_to` 有守卫，`migrate_to` 没有） | 初稿实测 |
| L1-2 | `cli.py:1196-1208`、`:361-364` | `export` 先截断输出文件再跑查询：查询失败/where 非法时旧文件被清空、`--out` 父目录被提前创建 | 初稿实测 |
| L1-3 | `cli.py:224/236/240/246-247` | 导入错误处理差：坏 JSONL 裸 traceback、UTF-8 BOM 三种格式全崩、CSV 多余字段触发内部 `AttributeError` | 初稿实测 |
| L1-4 | `cli.py:1237-1246` | 流式部分写入无汇总：第 N 批失败时已提交批次保留但零输出，用户无从知晓落了哪些行 | 初稿实测 |
| L1-5 | `cli.py:1414-1427`、`:1468-1470` | `repartition --dry-run` 不解析 interval/timezone：非法值预览成功、实跑才炸（且失败留下半成品目标库，`cli.py:1472-1487`） | 初稿实测 |

### UI

| # | 位置 | 问题 | 验证 |
|---|------|------|------|
| U1-1 | `app.py:600-635` | keyset 分页遇排序列 NULL 时 `Case(col,'>',None)` → 422，且 NULL 行永远不可达（SQLite NULL 排最前） | 初稿实测 |
| U1-2 | `tab-maintain.js:97-255,325-328` | 后端统一 `{report}/{validation}/{result}` 包裹，前端全部按根对象取值 → validate 恒显示“失败”、cleanup 预览恒空、compact/repartition 指标恒 `—` | 初稿实测 |
| U1-3 | `app.py:648-650` + `tab-query.js:98-99` | Query 的 Explain 面板未解包 `{plan}`，裁剪可视化恒退化 | 初稿实测 |
| U1-4 | `app.py:1022` | `Content-Disposition` 未转义：非 ASCII 表名导出 500（latin-1 编码错），含引号/换行表名生成畸形 header/断连 | 初稿实测 |
| U1-5 | `app.py:628-631` + `sqlite/table.py:2767-2769` | `/rows` 每页仍全表物化 + Python 排序（12 万行首页 0.15s、20M 行会 OOM），`MAX_PAGE_LIMIT` 只是假限流 | 初稿实测 |
| U1-6 | `tab-browse.js:283-288` | “上一页”缺 `s.pos--`，永远重复当前页 | 静态（明确） |
| U1-7 | 设计文档 `ui-20260823-overview.md:276`、`tests/test_ui.py:385` | 设计/测试声称存在的 `POST /api/table/{name}/import_buffer` 实际 404（只读测试反向断言掩盖） | 初稿实测 |

### PostgreSQL 引擎 + 查询层

| # | 位置 | 问题 | 验证 |
|---|------|------|------|
| P1-1 | `query_util.py:54-64`、`postgresql/table.py:871-872` | 字符串 `fields` 原样拼 SQL（两引擎共享，PG 多语句协议后果更重）；list 形式才引号化 | 初稿实测 |
| P1-2 | `postgresql/table.py:445` | 批量写列集合取 `data[0]`：异构行多余列静默丢弃、缺列 `KeyError` 且前序行已提交（SQLite 为 NULL 补齐） | 初稿实测 |
| P1-3 | `postgresql/table.py:78-83,368-369` | `commit` 参数被静默忽略（恒 autocommit）：批写入非原子、失败留半批脏数据（旧 P-M5 未修） | 静态 |
| P1-4 | `postgresql/table.py:584-594` | timestamptz 日历分区边界格式化为无时区字符串，按会话 TimeZone 解释，非 UTC tz 边缘行落错/报错（旧 P-M2 未修） | 静态 |

### utils/mysqlite + utils

| # | 位置 | 问题 | 验证 |
|---|------|------|------|
| M1-1 | `mysqlite/table.py:241-249` | P0-2 修复不完整：单行 fallback 路径 `ensure_column` 对 None 值把列类型建成字符串 `"None"` | 初稿实测 |
| M1-2 | `mysqlite/table.py:722-724,773-786` | `drop_table` 不失效 schema 缓存：以缓存重建表会丢失列类型/复活已删列及旧默认值 | 初稿实测 |
| M1-3 | `mysqlite/database.py:116-117` + `const.py:13` | `Database(wal=False)` 实际仍是 WAL（默认 pragma 覆盖） | ✅复核 |
| M1-4 | `mysqlite/connection_pool.py:195-218` | close/return 竞态：连接被塞回已关闭池，FD 与进程预算泄漏 | 初稿实测 |
| M1-5 | `mysqlite/connection_pool.py:171-179` | 同线程嵌套 checkout 永久死锁（`max_connections=1` 时 `get_connection()` 套 `transaction()` 即触发） | 初稿实测 |
| M1-6 | `mysqlite/table.py:384-400` + `connection_pool.py:206-209` | `auto_commit=False` / `insert(commit=False)` 静默丢写（与 `insert_all(commit=False)` 显式拒绝不一致） | 初稿实测 |

---

## 四、旧报告遗留项（`code-review-20260823-v030-full.md`，仍未修复）

以下条目上一轮已定性，本轮确认仍存在；部分已在上文升级/重述：

- **S-M1** `validate` 深校验 FD 泄漏 → 本轮 S1-4（✅复核）。
- **U-M1** 进程预算取所有池 `limit` 的 `min()`，一个保守池拖垮全进程（`connection_pool.py:30-32`，且被测试固化为期望）。
- **U-M2** `Database(timeout=...)` 被默认 `busy_timeout=30000` 覆盖（`database.py:91`）。
- **U-M3** `pragma()/enable_wal()` 只影响新建连接。
- **U-M4** `suggest_value_type` 不识别 numpy（`np.int64/np.float32/np.bool_` 均落 TEXT）。
- **P-M1** PG 契约不持久化 + `CREATE TABLE IF NOT EXISTS` 不校验漂移。
- **P-M3** PG 吞 `DuplicateTable` 掩盖分区边界重叠。
- **S-M3** `latest()/asof()` 全量物化（tick/orderbook 场景全表扫描）。
- **S-M11** 非 segment 布局每次查询对每分区做全表 COUNT + MIN/MAX。
- **S-M4** `mode="ignore"` 逐行探查 pk，写入放大。
- **S-M8** 孤儿 segment 文件 `validate()` 不反向扫描，且永久阻塞 `compact()`（`table.py:810-821,1208-1209`，初稿实测）。
- **S-M5** 字符串 `fields` 裸拼（CLI review 确认 sqlite3 单语句限制兜底，但 PG 侧见 P1-1）。

---

## 五、P2 摘要（按模块，详见复现脚本与源码）

**SQLite**：`write(mode="ignore")` 缺 pk 字段抛裸 `KeyError`；单列 integer 主键行缺字段被静默自增 rowid；`write(mode="upsert")`/`upsert_all` 在 pk 不含分区列时跨分区不更新产生重复行（需文档/校验兜底）；`Where` 空 AND 组语义错误（`where=[[], [c]]` 应为全集，实测返回仅 c）；`create_index(order="bogus")` 裸 `sqlite3.OperationalError`；`select(limit=True)` 当 1；`iter_rows` 返回 batch 列表与 README 的“逐行”描述不符；`close()` 在持有租约的线程内无限等待、关闭后 `_acquire_part_db` 可重开连接；死代码（`_table_columns` 为 pass 等）。

**核心元数据**：`migrate_to` 选项校验缺口（空表 `mode='bogus'` 静默成功、`tables=[]` 静默 0 表、`tables=123` 裸 TypeError）；`force_partition`/`TimeColumn` order_key 污染；`format_partition_spec` 对含引号列名不可回读；`PartitionSpec` 构造期 kind/every 校验缺失、异常族不统一；`parse_interval_ms(True)` 返回 True；旧/损坏 manifest 缺 key 时 `KeyError` 导致表打不开；`Segment(max_rows=100,target_rows=0)` 静默回落；`rename` 异常未包装、`rename_table` 先 rmtree 再 replace 不可回滚；`delete(None)` 无确认删全表；`utils/partition.py` 死模块；`Database.__getitem__` 的 `_tables` 无锁。

**CLI**：`--limit -1`、`--where @missing`、非法 URI 等库异常走裸 traceback 而非 `_fail`；退出码不统一（validate=2/index check=1/doctor=1）；`index create -c 不存在列` 仍报 created；`compact/cleanup --partition` 拼写错误静默 no-op；bytes 列 JSON 输出为 Python repr 不可逆；`--target` 可落在源库内部生成嵌套表；`--backup` 在源库内部触发 `OSError`；`query` 默认全量载入内存；`_write_rows` 死代码；`_backup_path` 秒级撞名。

**UI**：`QueryBody.start=dict`、`latest` 不存在列、`between/in` 元素为 dict 等导致 500 且异常文本外泄；CSV 公式注入（`= + - @` 前缀未转义）；空结果集 CSV 无表头；前端 `spec.pk` vs 后端 `primary_key` 字段错配；compact/cleanup 的 partition 任意类型静默成功；无 Host 校验（DNS rebinding 可绕过同源调写 API，`--host 0.0.0.0` 无警告）；Browse 请求无序号防竞态、菜单监听泄漏；`/query truncated` 假阳性；`/count`、`/chart` 同步长任务无超时/预算。

**PostgreSQL + query**：`_where_sql` 全局 `?`→`%s` 替换 + 手拼双引号（列名含 `?`/`%` 即坏）；NULL 排序与 SQLite 相反；`table_parts()` 返回关系名、regclass 解析脆弱、无排序；schema 硬编码 `public`；`blob/datetime` 映射为 PG 非法类型；文本日历边界补 `:00:00` 导致省略秒的值越界；`WITH HOLD` 游标在服务端物化全量；`Case.__eq__` 对非 Case 抛 AttributeError 且不可哈希；`Case.from_dict` 裸 KeyError。

---

## 六、测试缺口（按优先级）

1. **并发**：读线程 × delete/truncate；compact × delete/truncate 交错；`_return_connection` close 竞态；嵌套 checkout。
2. **破坏性 CLI**：restore 的 backup==target / backup⊂target / 真实 iosql 目标可覆盖 / symlink 目标；rename-database --overwrite 命中普通目录与根/家目录；migrate 同库；repartition 失败清理。
3. **契约冻结回归**：`select(to_dict=False)` 投影返回 tuple；`select(symbols=/start=)` 要么生效要么拒绝；`spec=` 绑定已有表和 `conflict/schema_policy` 生效；`Partition.calendar(every≠1)`；float 分区键等值查询；`fmt` 白名单；`4month` roundtrip。
4. **UI 后端**：NULL 排序列分页（或明确 422 hint）；非 ASCII/引号/换行表名导出；CSV 公式转义与空表头；`/repartition` 的 dry_run 语义（若保留）；非法 body 类型 422 而非 500；`import_buffer` 与文档一致性。
5. **mysqlite**：单行 `ensure_column` 的 None/表达式默认值；`insert_all` 表达式默认值；`auto_commit=False` / `insert(commit=False)`；`wal=False`；`drop_table` 缓存失效；复合索引 `drop_column_index`。
6. **PG**：本环境无 live PG 全部 skip，现有 FakeCursor 不模拟占位符绑定与 DDL 语义，P0-7/P0-8/P1-2/P1-3 无法被现有测试发现；建议至少补 Composable 渲染断言。
7. **发布产物**：现有测试全部从源码树导入，缺少 wheel/sdist 内容清单和隔离安装冒烟，因此没有发现 UI 静态资源完全缺包（P0-11）。

---

## 七、工程质量与发布状态

| 检查 | 结果 | 判断 |
|---|---|---|
| `python -m pytest tests/ -q` | 406 passed / 16 skipped / 68 warnings | 现有回归全绿，但没有覆盖本报告的关键交错、破坏性边界和发布产物 |
| `python -m compileall -q iosql examples benchmarks` | 通过 | Python 3.11 下语法与导入编译无误 |
| `python -m pip wheel . --no-deps` | wheel 构建成功 | 构建“成功”但产物不完整，见 P0-11；不能作为发布成功依据 |
| `ruff check iosql` | 20 项 | 核心包尚未达到基本 lint 绿色基线 |
| `ruff check iosql tests benchmarks examples` | 61 项 | 以未使用导入为主，另有类型比较、重定义、lambda 赋值等问题 |
| `pyright iosql` | 115 errors / 4 warnings | 当前类型标注不能作为质量门禁；部分是存量噪声，但也暴露 Optional、返回类型和连接参数的不一致 |
| CI/质量配置 | 未找到 `.github`、tox/nox、ruff/pyright/coverage 配置 | Python 3.9~最新版本、构建产物、lint 和类型检查没有持续门禁 |

其他发布/维护问题：

- `pyproject.toml` 声明支持 Python `>=3.9`，但本轮只在 Python 3.11 验证；仓库没有多版本测试矩阵。
- 未发现 LICENSE 文件或 `project.license` 元数据，公开发布前应明确许可证。
- `HANDOFF.md` 仍声称 PLAN-054 进行中且工作区有未提交改动，而当前代码已完成 PLAN-061、工作区基线为 clean；这会误导接手者。
- 401 个测试函数/约 423 个测试节点是良好基础，但 live PostgreSQL 测试依赖外部 `POSTGRES_DSN`，默认环境下跳过；前端没有浏览器级测试。

---

## 八、建议修复批次

- **批次 A（P0，立即）**：P0-1~P0-11。其中 P0-1/P0-2 是 SQLite 数据完整性根因（建议一起做），P0-3 是安全路径穿越，P0-4/P0-5/P0-6 是 CLI 破坏性防护（同一批重构 `_is_iosql_like_database` + 删除前改名回滚机制），P0-7/P0-8 是 PG 正确性/注入，P0-9/P0-10 是静默数据错误与“假预览”，P0-11 必须在下一个 wheel 发布前修复。
- **批次 B（P1 契约与并发缺陷，近期）**：S1-1~S1-6、C1-1~C1-7、L1-1~L1-5、U1-1~U1-7、P1-1~P1-4、M1-1~M1-6。
- **批次 C（旧报告遗留 + P2，按迭代）**：第四节与第五节；其中 `latest()` 物化（S-M3）与非 segment 全表聚合（S-M11）是已知性能主项。
- 建议批次 A/B 实施前生成 `docs/dev/PLAN-062-*.md`（可拆 PLAN-062/063），按 AGENTS.md 规范产出 OUTCOME。

---

## 附：本轮验证方式说明

- 初稿按六个领域完整通读；关键结论经独立复现的条目已标 **✅复核**。本次新增或重跑的确定性探针覆盖：并发读使 delete 静默回滚、compact 复活已删行、calendar fmt 越界写文件、restore 同源同目标自毁、真实库识别失败、select 业务参数被忽略、mysqlite 表达式默认值、UI 假 dry-run、PG delete/DDL 构造，以及 wheel 隔离安装。
- 未复现的条目按其证据强度分别标注「初稿实测」「静态推断」；标静态推断的 PG 行为需 live PG 环境最终确认。
- 产物级复核：构建 wheel、检查归档成员，并在干净临时目录中隔离安装后请求 UI 首页，确认 P0-11 不是 editable/source-tree 环境差异造成的假阳性。
- 基线测试全绿（406 passed / 16 skipped / 68 warnings）说明上述问题均在现有测试覆盖之外，而不是已由回归测试否定。
