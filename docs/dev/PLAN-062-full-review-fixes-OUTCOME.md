# PLAN-062 全量 Review 修复结果（OUTCOME）

- 日期：2026-09-18
- 计划：`docs/dev/PLAN-062-full-review-fixes.md`
- Review 依据：`docs/review/code-review-20260918-full.md`（P0×10、P1×35、P2 若干及旧报告遗留项）
- 基线：HEAD `0da5b0c`，测试 406 passed / 16 skipped
- 结果：**全量 670 passed / 16 skipped**（新增 264 个回归测试）；`examples/`（含 finance、mysqlite、mysqlite-scenarios）18+9 个脚本全部通过。
- 变更规模：42 个文件，+3485 / −962（含文档）

---

## 一、批次 A：P0（10 项全部修复并复现验证）

| # | 问题 | 修复 | 验证 |
|---|------|------|------|
| A1 | 并发读回滚 delete/truncate（共享 `auto_commit` 竞态） | `_delete_and_cases` 显式 `execute_params(commit=True)`；审计并补齐 `_overwrite_table_meta`/`upsert_part_column_range` 的显式 commit；mutation 不再依赖共享标志 | `test_concurrent_read_does_not_rollback_delete`（对旧代码实测失败） |
| A2 | compact 与 delete/truncate 不互斥（行复活） | delete/delete_all/truncate/create_index/drop_index/cleanup_obsolete 全部纳入 `_compact_lock`（RLock，与 write/compact 串行化） | `test_compact_and_delete_do_not_resurrect_rows`（旧代码实测复活） |
| A3 | calendar `fmt` 路径穿越 | `CalendarPartition` 收口 fmt 白名单（YYYY/YYYYMM/YYYYMMDD/YYYYMMDDHH/YYYYMMDDHHmm）；`_get_part_dbfile` 增加 containment 纵深防御抛 `UnsafeOperation` | 穿越 fmt/多级路径/fmt 表达式均被拒；`test_part_dbfile_containment` |
| A4 | CLI restore 目标身份判定失效（真实库误拒 + 伪造标记可删任意目录） | `_is_iosql_like_database` 改为「至少一个 `<表>/meta.sqlite3` 且 SQLite 文件头合法」；拒绝根/家目录、symlink | 真实库可覆盖；伪造 meta-only 目录拒绝且内容不动；symlink/root/home 拒绝（`test_plan062_cli`） |
| A5 | restore backup/target 重叠自毁 | 双向包含检查；替换改为「copytree 到临时目录 → `os.replace` 换入 → 删旧」，失败回滚旧目标 | 3 个重叠用例 + copytree 失败回滚用例 |
| A6 | `Database.rename --overwrite` 可删任意目录 | 源/目标身份校验（至少一个 `<表>/meta.sqlite3`）、根/家目录与 symlink 拒绝、先移目标到临时目录再删、OSError/EXDEV 包装与回滚 | 普通目录/非 iosql 源/symlink/root 拒绝、回滚恢复用例 |
| A7 | PG `delete` falsy where 全表删除 | 非 None 一律 `Where` 校验（空条件 `BadArgument`）；`None` 告警走 `delete_all`；返回 `rowcount` | `TestDeleteP0_7`（mock 渲染） |
| A8 | PG 列类型 DDL 注入 | 封闭 `_PG_TYPE_MAP`（补 blob→BYTEA、datetime→TIMESTAMP、timestamptz→TIMESTAMPTZ），未知类型 `BadArgument`，校验先于 DDL | 恶意类型串全部拒绝（mock 渲染） |
| A9 | mysqlite `insert_all` 表达式默认值字面量写盘 | 批量写入按 key 集合分组，缺列省略交由 SQLite 求值 `DEFAULT` | 表达式默认值与单行 insert 等价；字面量默认值可被覆盖 |
| A10 | UI repartition「预览」实际写库 | 删除假 dry_run 按钮，改为「执行重分区（写入新库）」+ 手输表名确认；写模型 `extra="forbid"`，未知字段 422 | `test_unknown_fields_rejected_422`、`test_repartition_works_without_preview` 等 |

## 二、批次 B：P1（33 项，按模块）

### SQLite 引擎
- **S1-1** partition setter 守卫：已有数据且配置不同 → `TableSpecConflict` + repartition 提示。
- **S1-2** `select/iter_select*` 拒绝业务 kwargs（`symbols/start/end/date/session`）与未知 kwargs，hint 指向 `query()/explain_select()`。
- **S1-3** manifest 引用但文件缺失：读路径统一抛 `UnsafeOperation`（含 validate/cleanup 修复指引），不再隐式重建空文件。
- **S1-4** `validate(deep=True)` FD 泄漏：改用 `contextlib.closing`；实测 100 分区 FD 回到基线。
- **S1-5** `select(to_dict=False)` 投影返回 tuple，兑现冻结契约。
- **S1-6** `db.table(name, spec=...)` 声明契约的 `conflict/schema_policy/symbol_column/session_column/order_key/force_partition/columns` 合并生效（`spec()` 惰性合并，兼容校验先行）。
- **附加**：字符串 `fields` 列名白名单+引号化、SQL 片段拒绝；空 AND 组恒真；ORDER BY+LIMIT 下推（每 segment LIMIT 后归并 top-N，结果与全量排序差分一致）；`limit` bool 拒绝；`create_index(order)` 校验；缺 pk 字段 `BadArgument`；`iter_rows` 改逐行 + 新增 `iter_batches`（migrate 改用后者）；孤儿文件 validate 报告 + compact 安全回收；`close()` 终态语义；`delete_all()` 显式化、`delete(None)` 告警。

### 核心元数据
- **C1-1** `Partition.calendar(every=...)` 生效（1week/4day 正确合桶，meta roundtrip）。
- **C1-2** duration/range 浮点键归一为 int（`0.0` 可被 `==0` 命中），bool/非法 width 拒绝。
- **C1-3** `migrate_to(dry_run=True)` 无落盘副作用（源表存在性先校验）。
- **C1-4** `Database.rename` 身份/特殊路径/回滚加固。
- **C1-5** `parse_interval_ms` 支持 `Nmonth`(30d)/`Nyear`(365d)、int/整值 float/数字串，拒绝 bool；doctor 建议可回读。
- **C1-6** `TimeColumn(unit='date')` 归一先转 timezone（沪/UTC 晚间接界用例）。
- **C1-7** `force_partition` 快捷路径透传；`spec=` 与 `conflict/schema_policy` 冲突即报错或显式覆盖；compat 校验含 symbol/session/order_key。
- **附加**：`PartitionSpec` kind/every/width 校验与异常族统一（`PartitionSpecError(BadArgument)`）；损坏 manifest 缺 key 报带修复指引的类型错误；`Segment`/`PhysicalLayout` 显式校验；`timeseries(time=TimeColumn)` order_key 不再污染；`rename_table` 可回滚；`_tables` 加锁；`utils/partition.py` 标注废弃；`format_partition_spec` 引号处理。

### CLI
- **L1-1** migrate 拒绝同库/嵌套目标（CLI + `Database.migrate_to` 双层守卫），`--verify row-count` 预检目标同名表。
- **L1-2** export 临时文件 + 原子替换，失败保留旧文件且不提前建父目录。
- **L1-3** import utf-8-sig、JSONL 行号报错、CSV 多列/缺列明确报错。
- **L1-4** 中途失败输出 `{error, rows_seen, rows_written, batches_committed}`（成功汇总同样补充）。
- **L1-5** repartition `--dry-run` 校验 interval/timezone/目标父目录并与实跑行为一致。
- **附加**：统一命令异常为 `_fail`（无裸 traceback）；退出码 0/1/2/130 并在模块 docstring 记录；`index create` 列校验；`compact/cleanup --partition` 校验并给可用分区提示；bytes JSON 改 base64；backup 重叠拒绝 + 命名加微秒/pid；删除 `_write_rows` 死代码；`iosql ui --host` 非回环警告。

### UI
- **U1-1** 排序列含 NULL：首页探测后 422 + hint，前端禁用该列排序，杜绝静默半截分页。
- **U1-2/U1-3** Maintain/Explain 响应解包修复（validate/cleanup/compact 指标与裁剪可视化恢复正确）。
- **U1-4** 导出文件名 RFC 5987 + ASCII 降级，中文/引号/换行表名 200 且无异常文本泄漏。
- **U1-5** 依赖核心 ORDER BY+LIMIT 下推；大表首页告警（fast_row_count），修正 `MAX_PAGE_LIMIT` 误导注释。
- **U1-6** Browse「上一页」补 `s.pos--`。
- **U1-7** `import_buffer` 从测试/设计文档移除（该端点不存在）。
- **附加**：写模型 `extra=forbid`；请求 body 类型校验 422（start/end/between/in/latest columns）；500 不回显异常文本；CSV 公式注入转义 + 空结果表头；`spec.primary_key` 字段对齐；`Compact/CleanupBody.partition` 类型限定；TrustedHostMiddleware；冻结的 `/query truncated` 修正；`/count` 500 万行扫描预算；前端请求序号防竞态、菜单监听/图表 resize 清理。

### PostgreSQL + 查询层
- **P1-1** 字符串 `fields` 仅接受标识符（PG 侧本地实现），SQL 片段拒绝。
- **P1-2** 批量写列并集 + 缺列 NULL 补齐（对齐 SQLite），多行 VALUES 分页。
- **P1-3** `commit` 参数落地：显式事务 BEGIN/COMMIT/ROLLBACK，失败整批回滚。
- **P1-4** timestamptz 日历边界带 `+00:00` offset；文本粗粒度边界兼容缺秒值。
- **附加**：`_where_sql` 占位符改写（引号内 `?`/`%` 安全）；NULL 排序 ASC NULLS FIRST / DESC NULLS LAST（与 SQLite 一致）；`table_parts` regclass/schema/排序/空表；`current_schema()` 替代硬编码 public；native 分区边界预检 + 冲突类型化；rename 事务化；契约漂移 `SchemaConflict`；`select` 未知 kwargs 拒绝；`upsert_all` where 改 keyword-only + 告警；WITH HOLD 改显式事务 + 非 hold 游标；file 死分支删除；索引名 63 字节截断 + 哈希；`Case` 可哈希 / `__eq__` 修正 / `from_dict` 类型化报错；`case_util` 超集消除修复。

### mysqlite + utils
- **M1-1** 单行 `ensure_column` None 值列类型不再是 `"None"`。
- **M1-2** `drop_table` 失效缓存 + 新增 `invalidate_cache()`；不再复活旧列/类型/默认值。
- **M1-3** `Database(wal=False)` 真正关闭 WAL；`enable_wal` 同时作用于空闲连接。
- **M1-4** close/return 竞态修复（锁内复查 `_closed`）。
- **M1-5** 同线程嵌套 checkout 重入租约（同连接引用计数），不再死锁。
- **M1-6** `insert/executemany(commit=False)` 显式拒绝；`auto_commit=False` 直写拒绝（走 `transaction()`）。
- **M1-7** 进程预算：空闲池不再压缩全局上限（取 max），有活跃池时按活跃池 min；更新既有测试。
- **M1-8** `timeout` 派生 `busy_timeout`（显式 pragma 优先）。
- **附加**：pragma 应用到空闲连接；numpy/numbers 类型推断；`suggest_column_types` 保留声明列；`DatabaseError` 捕获；`drop_all_index` 跳过 autoindex、`drop_column_index` 支持复合索引；`:memory:` 拒绝；create_table+meta 原子；fork 后 pid 检测重建；bytes 字面量 `X'..'`；schema `:memory:` 解析；`is_pool_healthy` 有实际语义；死代码清理。

## 三、批次 C：P2/旧报告遗留

- 已随批次 A/B 一并修复的旧项：U-M2（timeout）、U-M3（pragma 生效范围）、U-M4（numpy）、S-M1（FD 泄漏）、S-M8（孤儿文件）、S-M5（fields 注入，双引擎）。
- 明确保留（记录理由）：
  1. `latest()/asof()` 仍全量物化（S-M3）与**非 segment 布局每分区全表聚合**（S-M11）：属查询执行层重构，改动面大，未纳入本计划；当前已由 `select` ORDER BY+LIMIT 下推缓解 UI 主路径。
  2. `iter_batches` 仍用 OFFSET 翻页（单 writer 场景下稳定；并发 upsert/delete 下理论上可重复/漏行）。
  3. PG `schema` 取 `current_schema()`，表在 search_path 靠后 schema 时仍会误判（与旧硬编码 public 同样受限，已在 docstring 标注）。
  4. `delete([])` 在 SQLite 侧等价无过滤（删 0 行），仅 PG 严格拒绝；属引擎既有语义差异，已文档化。
  5. PG 无 live 环境：`_transaction(commit=False)` 的 autocommit 恢复、非 hold 游标大结果集内存表现、`pg_get_expr` 边界文本比较等 5 项需 live 冒烟。

## 四、新增回归测试

| 文件 | 数量 | 覆盖 |
|------|------|------|
| `tests/test_plan062_sqlite.py` | 45 | 并发 delete、compact 互斥、路径 containment、缺失文件、FD、投影、声明契约、下推差分、孤儿、close 语义等 |
| `tests/test_plan062_core.py` | 50 | calendar every/fmt、浮点键、dry_run、rename 守卫回滚、4month roundtrip、timezone、spec= 绑定、校验矩阵 |
| `tests/test_plan062_cli.py` | 34 | restore 身份/重叠/回滚、rename-database、migrate 守卫、export 原子、BOM/坏行/CSV、dry-run 校验、退出码、base64 |
| `tests/test_plan062_ui.py` | 33 | extra=forbid、无假预览、NULL 排序 422、解包、导出文件名/CSV 安全/空表头、Host、truncated、count 预算 |
| `tests/test_plan062_pg.py` | 66 | delete falsy、类型白名单、fields、列并集、事务回滚、tz 边界、NULL 排序、table_parts、schema、边界冲突、契约漂移、API 对齐、core query |
| `tests/test_plan062_mysqlite.py` | 36 | 表达式默认值、ensure_column、缓存失效、wal、close 竞态、嵌套重入、commit 拒绝、预算、timeout、fork、索引、:memory: |
| 既有测试适配 | — | `test_cli.py`/`test_migration.py`/`test_partition_integration.py`/`test_ui.py`/`test_connection_pool.py` 按新契约更新 |

## 五、文档同步

- `README.md`、`skills/iosql-usage/SKILL.md` 及 `references/{api-reference,troubleshooting,usage-patterns}.md`：接口行为、CLI 安全规则/退出码、UI 变更、PG/mysqlite 差异、新增 `delete_all`/`iter_batches`/`invalidate_cache`。
- `docs/design/ui-20260823-overview.md`：移除不存在的 `import_buffer`；补 Host/预算/无预览说明。
- `docs/design/ui-20260825-browse-filter-ux.md`：补 NULL 排序与上一页/竞态修复记录。
- `iosql/utils/mysqlite/README.md`：:memory:/commit/wal 等契约同步。

## 六、收尾补丁（主审）

- PG `create_index` 恢复 `sql.Identifier` Composable 构造（保留 PLAN-056 注入防护测试），同时保留索引名 63 字节截断 + 哈希。
- `Database.migrate_to` 增加库级同库/嵌套目标守卫（CLI 之外的双保险）。
- `iosql ui --host` 非回环地址显式告警。

## 七、验收

- `python -m pytest tests/ -q` → **670 passed, 16 skipped**（无失败，无新增 skip）。
- examples 全量冒烟通过（18 个主示例 + 9 个 mysqlite 示例）。
- 修复项均有对应回归测试；P0 高危项在旧代码上可复现失败。
