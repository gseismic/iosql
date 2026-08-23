# iosql v0.3.0 全量代码 Review 报告

- 日期：2026-08-23
- 基线：HEAD = `faba3a6`（实施 PLAN-054: 用户接口定稿）
- 复核修订：2026-08-23 第二轮外部核实（约 55 条论断，49 条完全属实），本版已修正 6 处：P0-8 的 PG 对比说法（两引擎建表均不校验表名）、CLI `--limit` 负值说法（引擎已拒绝，详见第四节更正说明）、`ensure_columns` 与 P0-9 的路径标注、C-M1 行号（1239→1237）、`where=[]` 定性（确定的全表查询而非未定义）。各处修正均以"更正说明/已更正"标注在原条目内。
- 范围：全部已提交代码，约 13,000 行 Python。包括核心 API（`database.py`/`table.py`/`spec.py`/`exception.py`）、查询层（`core/query/*`）、分区层（`partition/*`）、SQLite 引擎（`engines/sqlite/*`）、PostgreSQL 引擎（`engines/postgresql/*`）、底层工具（`utils/mysqlite/*`、`utils/schema.py` 等）、CLI（`iosql/cli.py` HEAD 版本 + `cli/` 兼容包）。
- 排除：UI 相关未提交内容（`iosql/ui/`、`tests/test_ui.py`、`iosql/cli.py`/`README.md`/`pyproject.toml` 的未提交改动）、`tests/`、`benchmarks/`、`examples/`、`skills/`。
- 方法：核心层与查询/分区层逐行人工 review；SQLite 引擎、PostgreSQL 引擎、mysqlite、CLI 四大块由并行深度 review 完成并经人工抽查验证关键结论（本文标注 ✅ 的条目均经二次验证：直接读源码或实测复现）。

---

## 一、总体结论

工程化水平明显高于平均水平：参数化 SQL 与标识符转义的主体用法正确、租约 + LRU 连接管理、二级裁剪（partition + segment stats）、obsolete/generation 生命周期、dry-run 默认 + `--yes` 双确认的 CLI 安全风格都经得起推敲。查询 OR 语义实现为单 SQL `or` 组合（`query_util.py:126-139`），不存在跨分支重复行问题。

但存在 **3 类必须在下个版本前修复的问题**：

1. **多线程写 + compact 并发竞态**（数据重复/丢失窗口）；
2. **破坏性路径防护不完备**（`Database.rename` 包含关系单向检查、CLI `restore --replace` 无目标校验、SQLite 建表表名可目录逃逸、PG `create_index` SQL 注入）；
3. **两条静默数据错误路径**（mysqlite `ensure_column` 补列一律建成 TEXT、PG `iter_select_batches` 无序 OFFSET 翻页在迁移中可静默拷错数据）。

此外非 segment 布局每次查询全表聚合、`latest()` 全量物化两处性能陷阱，与文档主推场景（tick/orderbook）直接冲突。

### 问题统计

| 模块 | 严重 | 中等 | 轻微 | 建议 |
|------|-----|-----|-----|-----|
| SQLite 引擎 | 2 | 11 | 9 | 4 |
| PostgreSQL 引擎 | 4 | 10 | 9 | 2 |
| utils/mysqlite + utils | 3 | 8 | 9 | 3 |
| CLI | 1 | 4 | 7 | 3 |
| 核心 API（database/spec/table） | 1 | 2 | 5 | 1 |
| 查询层 + 分区层 | 0 | 0 | 4 | 1 |

---

## 二、P0：数据正确性与安全（建议立即修复）

### P0-1 SQLite：写入与 compact 并发竞态，segment 可被"复活"或晚到行永久丢失 ✅
- 位置：`iosql/engines/sqlite/table.py:2290-2306`（`_increment_segment_info`）、`783-806`（compact 持 `_compact_lock`）、`2388-2409`（`_append_segment_rows` 不持锁）、`1123`（`_mark_segments_obsolete`）
- 写路径拿到 segment S 后，compact 可并发完成"读 S → 写新 generation → 标记 S obsolete"。此后写路径仍向 S 插入并通过 `_increment_segment_info` 回写 manifest，其中 `state='sealed' if sealed else 'active'`（table.py:2303）**无条件计算 state，不保留 obsolete 态**（对照 `_refresh_segment_info:2177-2181` 是保留的）→ S 被复活，旧数据 + 新 generation 同时可见，**行重复**。另一时序（写文件成功后 compact 才标记）则晚到行留在 obsolete segment，`cleanup_obsolete(dry_run=False)` 删文件后**永久丢失**。`_compact_lock` 只保护 compact 自身，写路径完全不入锁；lease 机制只保护连接缓存。
- 修复：① `_increment_segment_info` 统一保留 obsolete 态，检测到写入 obsolete segment 时报错或重路由；② 写路径与 compact 共用锁；③ 至少在文档与 API 层强制单 writer 并显式声明。

### P0-2 mysqlite：`ensure_column` 批量补列路径类型推断失效，新列一律建成 TEXT ✅
- 位置：`iosql/utils/mysqlite/table.py:642-654`（`_ensure_columns_on_connection`），调用链 `insert_all(ensure_column=True)` → `_insert_one_batch_on_connection:567-569`
- `od_fields` 来自 `getarg_diclist_insertmany` → `_union_fields_of_dictlist`（`sql.py:46-61`），值是**类型字符串**（`'integer'/'float'`）；但 `table.py:647` 对它调用 `suggest_value_type(value)`——对字符串入参恒返回 `'text'`。结果：触发 NoSuchColumn 回退补列时，数值列全部被 `ALTER TABLE ADD COLUMN ... text` 建成 TEXT 亲和列，数值以字符串落盘，破坏后续数值比较/排序。对比同文件旧路径 `ensure_columns`/`ensure_column`（`table.py:237-239`）用的是 `type_ or 'text'`（正确），`_on_connection` 变体引入了回归。（初版误将 `ensure_columns` 标注为"484-522 行附近"——484-522 实为死代码调用方 `_insert_one_batch`，已更正。）
- 修复：`type_ = value if value in ('integer','float','text','blob') else (suggest_value_type(value) or 'text')`。

### P0-3 PostgreSQL：`create_index` SQL 注入 ✅
- 位置：`iosql/engines/postgresql/table.py:1029-1033`
- `index_name`/表名/列名直接 f-string 拼进 `CREATE INDEX`，同文件其他方法均用 `sql.Identifier`。psycopg2 无参数时走简单协议，单次 execute 允许多语句，且连接 autocommit（table.py:69），`column='id); DROP TABLE x; --'` 可直接执行。
- 修复：`sql.SQL('CREATE INDEX IF NOT EXISTS {} ON {} ({})').format(sql.Identifier(...), ...)`，order 用白名单映射。

### P0-4 PostgreSQL：`iter_select_batches` 无 ORDER BY 时 OFFSET 翻页不正确，迁移可静默拷错数据
- 位置：`iosql/engines/postgresql/table.py:711-739`，`iter_rows:753` → `migrate_to:800` 复用
- `order_by` 可选，缺省时靠 OFFSET 翻页；PostgreSQL 不保证跨语句行序稳定，会跳行/重复。SQLite 引擎走按分区稳定扫描（`sqlite/table.py:917-946`）无此问题，PG 迁移路径却可能静默拷错数据（`verify='row-count'` 只在计数恰好一致时才漏过，且此时数据已污染）。
- 修复：无 order_by 时强制追加确定性排序键（pk 或分区列），或改 keyset 分页（顺带解决深分页 O(n²)）。

### P0-5 PostgreSQL：file 分区模式写入/读取分裂，数据写后不可见
- 位置：`iosql/engines/postgresql/table.py:290-293`（建普通空表）、`626-656`（写本地 SQLite 文件）、select/count_rows/delete 永远只查 PG 主表
- file 模式经公开 API 不可达（`Database.__getitem__` 不透传 `partition_mode`），结合读写分裂，实为不可用死路径；但一旦被内部代码启用即静默丢读。
- 修复：删除 file 分支或补齐读写一致性与 API 透传。

### P0-6 CLI：`restore --replace --yes` 可删除任意目录，无目标类型校验（已实测）
- 位置：HEAD `iosql/cli.py:1521-1529`
- 目标存在且双 flag 时直接 `shutil.rmtree(target_path)`，不校验是否 iosql 库、是否根/家目录。对比 `Database.rename` 有包含关系防护、`cleanup_obsolete` 有 `_is_safe_part_path`，唯独最危险的覆盖路径裸奔。
- 修复：覆盖前校验目标目录结构像 iosql 库并拒绝特殊路径；更稳妥是先 rename 旧目标到临时位置，成功后再删（可回滚）。

### P0-7 核心 API：`Database.rename` 包含关系检查单向，overwrite 可删掉源库自身 ✅
- 位置：`iosql/database.py:363-376`
- 只检查"target 在 source 内"（`realpath(target).startswith(realpath(source)+os.sep)`），未检查"source 在 target 内"。`source=/a/b/db, target=/a` 且 `overwrite=True` 时：检查通过 → `shutil.rmtree('/a')` **连带删除源库**，随后 `os.replace` 失败，数据已毁。
- 修复：增加反向包含检查 `realpath(source).startswith(realpath(target)+os.sep)` 时抛 `UnsafeOperation`。

### P0-8 SQLite：建表表名未校验，可目录逃逸（两引擎建表路径均不校验）✅（已复核修正）
- 位置：`iosql/engines/sqlite/table.py:310`（`os.path.join(self.db_path, self._table_name)`）
- `db.table("a/../../evil")` 直接拼进文件系统路径，`Database.__getitem__` 上游也无校验。`_validate_rename_table_name`（378-388）只在 rename 时校验。
- 更正说明：初版报告称"PG 引擎建表处校验表名（postgresql/table.py:131）"，经复核不成立——PG 的 `_validate_relation_name`（`postgresql/table.py:126-132`）唯一调用点是 `rename()`（:79），`create_table`（:200-236）同样不校验。即**两引擎建表路径都不校验表名**；SQLite 侧因涉及文件系统路径可目录逃逸，风险实际；PG 侧仅影响关系名合法性（`schema.table` 形式可改写 schema），风险较低。
- 修复：两引擎统一在 `Database.table()` / 引擎 `connect()` 入口校验表名（SQLite 侧禁 `/`、`..`、`__` 前缀）。

### P0-9 mysqlite：`IntegrityError` 逃逸且不回滚，脏连接归还入池
- 位置：`iosql/utils/mysqlite/table.py:310-393`（`_execute_params_on_connection`/`_executemany_on_connection`（329-343）、`execute`（345-354）、`executemany`/`fetchall`/`fetchone`（361-393）只捕获 `OperationalError`，executemany 系列多捕一个 `ProgrammingError`）；`connection_pool.py:195-209`（`_return_connection` 归还前不 rollback）
- UNIQUE/NOT NULL 冲突抛出的 `sqlite3.IntegrityError` 不走 `_handle_error_on_connection` 的 `conn.rollback()`；带未提交事务的连接被还进池子，之后任意一次同连接 `commit()` 会把半批数据意外提交。`_return_connection` 的 `SELECT 1` 存活检查对未决写事务同样成功，起不到兜底作用。
- 修复：捕获 `sqlite3.Error` 统一回滚 + 转换；`_return_connection` 在存活检查前无条件 `conn.rollback()`（无事务时是廉价 no-op）。

---

## 三、P1：正确性与一致性（建议近期修复）

### SQLite 引擎
| # | 位置 | 问题 |
|---|------|------|
| S-M1 | `sqlite/table.py:1429-1468` | `with sqlite3.connect(path) as conn` 只是事务上下文不关闭连接；WAL 下 `-wal/-shm` 残留，Windows 句柄泄漏会令 `cleanup_obsolete` 的 `os.remove` 失败。改 `contextlib.closing` |
| S-M2 | `table.py:3129-3148, 917-946` | OFFSET 翻页无 ORDER BY，跨语句行序不保证；深分页 O(n²)。改 rowid keyset 或游标流式 |
| S-M3 | `table.py:1522-1562` | `latest()/asof()` 把所有 `ts <= as_of` 匹配行读入内存排序后每组取首条，tick/orderbook 场景等于全表扫描。应下推 `ORDER BY ts DESC LIMIT n` 或窗口函数 |
| S-M4 | `table.py:3452-3502` | ignore 模式逐行 × 每 segment 探查 pk + 每行全量读 manifest，10 万行 × 20 segments ≈ 200 万次查询。需批量化 |
| S-M5 | `query_util.py:54-64` + `table.py:2800-2823` | 字符串 `fields` 原样拼入 SQL（`select("symbol from data; --")` 可注入分号后内容）；文本 where 有参数化解析，fields 却裸拼，同一 API 防护标准不一致 |
| S-M6 | `table.py:1097-1101, 3905-3911` | compact/repartition_to 整分区载入内存（与 migrate_to 的流式不一致），大分区 OOM 风险 |
| S-M7 | `table.py:2737-2768` | `select()` 无条件全量物化，无 order_by + limit 时 limit 不下推到每 segment SQL |
| S-M8 | `table.py:2373-2380, 2444-2451, 808-819` | 数据落盘与 manifest 登记之间的 crash 窗口产生孤儿文件；validate 不反向扫描 `data/` 目录，无法发现；stats 过期时裁剪可能错误跳过 segment（select 与 delete 共用） |
| S-M9 | `table.py:367-375` | rename 目录替换与 manifest 更新非原子，中途崩溃后表"消失"（数据在但 `where table_name=?` 查空），无恢复入口提示 |
| S-M10 | `table.py:4062-4090` | `_acquire_part_db` 在条件锁内做文件打开 + PRAGMA，且 0.1s 轮询忙等，多分区并发导入的吞吐瓶颈 |
| S-M11 | `table.py:2453-2493, 2050-2152` | 非 segment 布局（含默认单文件）每次查询对每分区做全表 COUNT + 全列 MIN/MAX 聚合，完全不缓存，select 前先 ≥2 次全表扫描/分区 |

### PostgreSQL 引擎
| # | 位置 | 问题 |
|---|------|------|
| P-M1 | `table.py:180-198` | 契约不持久化，进程重启后 `write()/migrate_to()` 报 BadArgument；`CREATE TABLE IF NOT EXISTS` 对同名表不校验 schema 漂移，静默放过 |
| P-M2 | `table.py:571-581` | timestamptz 列分区边界格式化为无时区字符串，按会话 TimeZone 解释；会话 TZ ≠ partition.tz 时边缘行落不进分区报错。边界应带 offset 或先 `SET timezone` |
| P-M3 | `table.py:535-541` | 吞掉 `DuplicateTable` 会掩盖"分区边界重叠"错误，行被静默路由进重叠旧分区 |
| P-M4 | `table.py:88-124` | rename 多语句在 autocommit 下逐句提交，中途失败留半改名状态 |
| P-M5 | `table.py:69, 295-334, 435-483` | `commit` 参数被静默忽略（恒 autocommit），批量写非原子；且逐行 INSERT + 每行重建 SQL Composable，上万行 = 上万次网络往返，应改 `execute_values` |
| P-M6 | `table.py:379-383, 1040-1055` | 以 `data[0]` 列集合为准：异构行多余列静默丢弃、缺列 KeyError 且前序行已提交 |
| P-M7 | `table.py:1001-1021, 1078-1096` | 与 SQLite 契约漂移：delete 返回 None（SQLite 返回行数）、`where=None` 无废弃警告；`table_parts()` 返回关系名而非分区键 |
| P-M8 | `table.py:979-999` | `upsert_all` 新增 SQLite 没有的 `where` 参数，且内部调用已废弃的 `select_all` 每次触发 DeprecationWarning |

### mysqlite / utils
| # | 位置 | 问题 |
|---|------|------|
| U-M1 | `connection_pool.py:30-32` | 进程级预算取所有池 limit 的 `min()`，一个保守池（如测试临时库）拖垮全进程连接上限 |
| U-M2 | `database.py:92` + `connection_pool.py:114-116` + `const.py:19` | `Database(timeout=...)` 构造参数被 `_DEFAULT_PRAGMA` 的 `busy_timeout=30000` 覆盖，用户设置静默失效 |
| U-M3 | `database.py:124-147, 495-505` | `pragma()/enable_wal()` 只影响未来新建连接，对存量池化连接无效；`_ensure_all_pragma` 全库无调用（死代码），API 有误导性 |
| U-M4 | `sql.py:124-134` | `suggest_value_type` 不识别 numpy 标量（int64 → text），金融管道数值列建成 TEXT；函数注解 `-> str` 实际可返回 None |
| U-M5 | `column.py:60-79` | `suggest_column_types`：数据带未声明字段走推断路径后，已声明但数据未出现的列被静默丢弃 |
| U-M6 | `database.py:438/457/492` | `except sqlite3.Error` 永不命中（`execute` 已转成 `DatabaseError` 抛出），三处上下文包装是死分支 |
| U-M7 | `partition.py:125-128` | `assert partition[:3] == 'KTS'` 拒绝所有 TS* 一等公民分区，紧随的警告分支永不可达（遗留模块，仅 tests 使用） |

### CLI
| # | 位置 | 问题 |
|---|------|------|
| C-M1 | HEAD `cli.py:1237` | `import --mode ignore` 虚报 `rows_written`（已实测复现）：engine 返回 `rows_written=None`（`sqlite/table.py:618` ✅），CLI 却累加 `rows_seen`，跳过的行也计入 |
| C-M2 | 全文件 | 库异常（BadArgument/UnsafeOperation 等）未统一捕获，输出 rich traceback 而非 `_fail` 的单行错误风格（退出码仍非零，已实测） |
| C-M3 | `cli.py:1423-1425, 1506-1509` | `repartition/backup --target` 允许目标在源库目录内（`Database.rename` 明确禁止）；目标目录会被 `table_names()` 当作逻辑表列出，污染元数据 |
| C-M4 | `cli.py:1486-1488` | `repartition --replace-source` 流程为 copytree → rmtree(源) → move(tmp→源)，rmtree 与 move 之间失败则源库消失；配 `--no-backup` 彻底丢失。改两次 `os.replace` 原子化（备份也近乎零成本） |

### 核心 API
| # | 位置 | 问题 |
|---|------|------|
| D-M1 | `database.py:521-531` | `_migration_table_mapping([])` 返回空 dict → migrate_to 迁移 0 张表"成功"返回（CLI `--table " "` 可触发，已实测）；空列表应报错 |
| D-M2 | `database.py:309-344` | SQLite `table_names()` 把目录下所有非 `__` 开头子目录当表，无任何标记文件校验（与 C-M3 联动放大） |

---

## 四、P2：轻微问题与死代码（择机清理）

### 查询层 / 分区层 / spec（本报告新增）
- `core/query/case.py:145-146` — `Case.__eq__` 对非 Case 对象抛 AttributeError 而非返回 NotImplemented；定义 `__eq__` 后 Case 不可哈希。
- `core/query/case_util.py:34-76` — `unique_or_and_cases` 生产死代码（仅测试引用）；且超集消除只比较 `j < i`，`[[A,B],[A]]` 顺序时漏消除冗余分支（OR 语义仍正确，仅浪费查询）。建议删除。
- `core/query/where.py:55-58` — `where=[]` → `_parse_sequence_where` 返回 `[]`，`select()` 落入 `if not w.or_and_cases` 分支（`sqlite/table.py:2738-2740`）与 `where=None` 等同走**全表查询**。语义确定但反直觉：非 None 的空条件列表静默等价于无条件全表扫描（初版误称"语义未定义"，已更正）。建议显式报错或文档声明。
- `iosql/spec.py:309-322` — `TimeColumn.normalize_value` / `partition/calendar.py:199-202`：`int(timestamp * 1000)` 浮点截断在边界值可差 1ms，应用 `round()`。
- `iosql/spec.py:513-516` — `parts_for` 对混合类型分区键 `sorted` 抛裸 TypeError。
- `TimeColumn.normalize_value` 对非法日期字符串抛 `arrow.parser.ParserError`，不属于 `IosqlError` 异常体系。
- `partition/base.py:110` — `from_meta` 未知类型抛 `ValueError` 而非 `PartitionSpecError`，异常家族不一致。
- `text_where.py` 实现质量高（转义、优先级、括号均正确）；仅"关键字不可作字段名"未在文档声明。

### SQLite 引擎
- 死代码：`table.py:628` `_parts_for_runtime_partition`、`3707` `_table_columns`（空 pass）、`3825/3828` `_partition_row_count/_partition_time_range`、`1998` `_segment_created_at`、`query_util.py:160/176` `build_select_cases_sql/build_delete_sql`。
- `table.py:2770-2776` — `_normalize_limit` 不拒 bool（`limit=True` → 1），与 `_normalize_batch_size` 双标。
- `table.py:319-340` — `close()` 在持有租约的线程内调用会无限等待；meta 连接无租约保护。
- `table.py:3530` — pk 一致性检查用 set 比较，忽略列顺序。
- `table.py:3856-3860` — `layout_report` undersized 判定用全局 segment 总数而非同分区维度。
- `table.py:917-946` — `iter_rows` yield 的是 batch 列表而非行，名不副实。

### PostgreSQL 引擎
- `table.py:898` — `_where_sql` 全局 `replace('?', '%s')` 破坏含 `?` 的列名/字面量。
- `table.py:13,15` — 死导入；`746-750` — `hasattr(cursor,'fetchone')` 恒真死分支。
- `table.py:147,168,914,1095` — schema 硬编码 `'public'`；`%s::regclass` 对大写/特殊字符表名解析失败。
- `table.py:619-624` — 分区表名未考虑 PG 63 字节标识符上限，超长静默截断撞名。
- `table.py:592-617` — `_calendar_text_bounds` 用本批首样本猜文本格式，混合格式破坏字典序 range。

### mysqlite / utils
- `backup/_helpers.py` 整个文件为死代码（全仓库无 import），且自身有 DEFAULT 值处理 bug，建议删除。
- `mysqlite/table.py` 死代码群：`_insert_one_batch`(484-522)、`_list_insertmany`(701-713, 含 `assert 0`)、`_try_commit`(792-805)、`_handle_error`(260-279)。
- `mysqlite/database.py:377,412` — `WHERE type="table"` 双引号字符串字面量靠 SQLite 回退语义才成立。
- `mysqlite/table.py:38-41` — 错误消息正则 `(\w+)` 不匹配带连字符/引号标识符。
- `utils/schema.py:118` — `':memory'` 判断永远匹配不到标准 `:memory:`。
- `sql.py:21-28` — `sql_literal` 不处理 bytes；空 columns 生成非法 SQL 仅靠调用方前置守卫。

### CLI
- 退出码语义不一致（validate=2 / index check=1 / doctor=1）；`export` 查询前先截断已存在输出文件，失败时原文件已毁（应临时文件 + rename）；`restore` 用 `shutil.rmtree` 而非 `_rmtree_robust`（FUSE 场景会失败）；JSONL 坏行裸 traceback；死代码 `_read_rows`/`_write_rows`。
- 更正说明：初版所列"`--limit` 不拒负值"不成立——引擎 `_normalize_limit`（`sqlite/table.py:2770-2776`）对负值抛 `BadArgument`，`select`（:2730）与 `iter_select_batches`（:2646）均调用，负值端到端会被拒绝。真实残留问题只是 CLI 层未前置校验：负值以未捕获 `BadArgument` 的 traceback 形态呈现（属 C-M2 的具体实例），且 `export` 场景下输出文件此时已被先截断。

---

## 五、修复路线图建议

1. **PLAN-A（数据安全，P0 全部 + C-M1/M4）**：S1 竞态、mysqlite 补列回归 + IntegrityError 回滚、PG create_index/翻页/file 模式、rename 双向包含、restore/rename 目标校验、表名校验、CLI ignore 统计与 replace-source 原子化。每项配回归测试（尤其 S1 需要并发写 + compact 的压力测试）。
2. **PLAN-B（性能，S2/M3/M4/M7/M11 + P-M5）**：legacy 布局聚合缓存、latest 下推、ignore 批量化、limit 下推、PG execute_values。
3. **PLAN-C（一致性清理，P1 剩余 + 死代码）**：PG 契约持久化与时区边界、跨引擎契约对齐（delete/table_parts/upsert_all）、CLI 异常统一捕获与退出码规范、死代码批量删除。

## 六、值得肯定的设计

- 查询编译层（`query_util.py`）参数化彻底，标识符统一 `_quote_sql_identifier`，OR 组合单 SQL 无重复行。
- `spec.py` 的 partition 字符串规范（parse/format 对称 + 特殊字符引号转义）设计严谨，round-trip 安全。
- `calendar.py` 的 lru_cache + 非 hashable 兼容路径、DST 一致的 bucket 计算，`text_where.py` 的手写 tokenizer/parser 均为高质量实现。
- 连接池 `_created/_checked_out` 计账在各异常路径（含 close 竞态）保持平衡；弱引用注册表不延长对象生命周期。
- CLI 破坏性操作普遍 dry-run 默认 + `--yes` 双确认的统一安全风格。
