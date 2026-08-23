# PLAN-056 结果: v0.3.0 全量 Review 修复（P0 + C-M1/C-M4）

日期: 2026-08-23
计划: docs/dev/PLAN-056-v030-full-review-fixes.md
依据: docs/review/code-review-20260823-v030-full.md
状态: COMPLETE（P0 全部 9 项 + C-M1/C-M4 完成，全量回归通过）

## 变更清单

### P0-1 SQLite 写/compact 并发竞态（`engines/sqlite/table.py`）
- `do_insert_all` 写循环整体包进 `_compact_lock`，与 `compact()` 串行化，消除
  "compact 标记 obsolete 后写路径仍向旧 segment 插入" 的复活竞态与晚到行丢失窗口。
- `_increment_segment_info` 的 `state` 计算改为保留 obsolete 态（与
  `_refresh_segment_info` 对齐），不再无条件重算为 active/sealed 复活旧 segment。
- `_compact_lock` 为 RLock，compact 内部 `_write_compact_chunks` 走
  `_write_segment_rows`（不经 `do_insert_all`），无重入问题。

### P0-2 mysqlite 批量补列类型推断（`utils/mysqlite/table.py`）
- `_ensure_columns_on_connection`：`od_fields` 值是类型字符串
  （`integer/float/text/blob`），白名单直接采用；否则回落 `suggest_value_type`。
- 顺带修复同函数第二个缺陷：直接 `cursor.execute` 抛的是基础
  `sqlite3.OperationalError`，原 `except DuplicateColumn`（其子类）永不命中，
  已有列会导致补列路径直接崩溃——改为捕获 `sqlite3.OperationalError` 且消息含
  `duplicate column` 时跳过。

### P0-3 PG `create_index` SQL 注入（`engines/postgresql/table.py`）
- `index_name`/表名/列名全部改用 `sql.Identifier`，order 走白名单
  （`asc/desc`），非法 order 抛 `BadArgument`，杜绝 f-string 注入。

### P0-4 PG `iter_select_batches` 无序 OFFSET 翻页（`engines/postgresql/table.py`）
- 改为单语句命名游标（`WITH HOLD` + autocommit）流式读取：单语句快照 + 服务端
  游标，消除跨语句行序不稳定导致的跳行/重复；`limit` 仍在单语句 SQL 中下推。
- 游标名用模块级 `itertools.count` 保证唯一，避免同连接并发迭代撞名。
- `_select_query` 的 `offset` 参数保留（无调用方再传，文档标注为兼容保留）。
- 迁移路径（`migrate_to`/`iter_rows`）自动受益，`verify='row-count'` 不再有
  "计数恰好一致但数据已污染" 的漏检窗口。

### P0-5 PG file 分区模式（`engines/postgresql/table.py`）
- `__init__` 入口：`partition_mode='file'` 直接抛 `BadArgument`（读写分裂死路径，
  写本地 SQLite 文件而读只查 PG 主表）；非法 `partition_mode` 一并拒绝。
- 不删除 file 分支代码（死代码清理留 PLAN-C）。

### P0-6 CLI `restore --replace --yes` 目标校验（`cli.py`）
- 覆盖前校验：目标必须是目录、拒绝根/家目录、必须含 `meta.sqlite3`
  （`_is_iosql_like_database`）才允许删除；删除改为"先 `os.replace` 到临时名，
  成功后再 `_rmtree_robust`，失败回滚"（可恢复）。

### P0-7 `Database.rename` 双向包含检查（`database.py`）
- 原只检查 target 在 source 内；新增反向检查 source 在 target 内
  （`source_real.startswith(target_real + os.sep)`）抛 `UnsafeOperation`，
  堵住 `overwrite=True` 时 `rmtree(target)` 连带删除源库的路径。

### P0-8 SQLite 建表表名校验（`engines/sqlite/table.py`）
- `_validate_rename_table_name` 更名 `_validate_table_name`，并在 `connect()`
  入口调用（`super().__init__` 触达），拒绝空名、`.`/`..`、`__` 前缀、路径分隔符，
  堵住 `db.table("a/../../evil")` 目录逃逸。

### P0-9 mysqlite IntegrityError 回滚 + 脏连接（`utils/mysqlite/table.py` + `connection_pool.py`）
- `table.py` 全部连接路径的 `except sqlite3.OperationalError` /
  `(OperationalError, ProgrammingError)` 统一改为 `except sqlite3.Error`
  （IntegrityError/NotSupportedError 等全部覆盖），经
  `_handle_error_on_connection` 先 `rollback()` 再转换/抛出。
- `connection_pool._return_connection`：归还前无条件 `conn.rollback()`（无事务
  时是廉价 no-op），清除脏连接上的半批事务，堵住"之后任意 commit 意外提交"。
- 连带修复：mysqlite `Table.auto_commit` 从构造时缓存改为动态 property
  （读 `self.database.auto_commit`），消除 `_acquire_part_db` 改
  `db.auto_commit` 后 Table 侧缓存过期的隐患（修复回归中暴露的 truncate 依赖
  auto_commit 提交 DELETE 却因缓存过期未提交的问题）。

### C-M1 CLI import ignore 模式 rows_written（`cli.py`）
- 不再累加 `result.rows_seen` 冒充写入数；`rows_written` 按
  `result.rows_written` 聚合，ignore 模式引擎返回 None 时如实输出 `null`。

### C-M4 CLI repartition replace-source 原子化（`cli.py`）
- 备份改为 `os.replace(source→backup)`（原子改名，近零成本，替代 copytree）；
  跨文件系统 `--backup` 抛 `EXDEV` 时明确拒绝（原子备份不支持跨盘，源库原封不动）。
- 替换改为 `os.replace(target→source)`（`--target` 与 `--replace-source` 互斥，
  目标恒为源库父目录下的临时路径，恒同盘），失败回滚；`--no-backup` 用临时名移走
  旧源、替换成功后删除，消除 rmtree 与 move 之间的源库消失窗口。
- 复核轮曾引入 `_move_dir` 跨盘 copy 回退以兼容旧 copytree 行为；经权衡后删除——
  跨盘备份无法原子，与 C-M4 原子化意图相悖，明确报错优于静默慢拷贝。

## 回归测试

新增 `tests/test_plan056_fixes.py`（9 例）+ `tests/test_cli.py` 追加 2 例：

| 用例 | 覆盖 |
| --- | --- |
| `test_mysqlite_insert_all_ensure_column_preserves_numeric_type` | P0-2 数值补列为 integer |
| `test_postgresql_file_partition_mode_rejected` / `invalid_partition_mode_rejected` | P0-5 |
| `test_database_rename_rejects_target_containing_source` | P0-7 反向包含 + 源库存活 |
| `test_sqlite_table_name_validation_rejects_escape` | P0-8 五种非法表名 |
| `test_mysqlite_integrity_error_rolls_back_connection` | P0-9 失败后连接干净、后续写入可提交 |
| `test_write_compact_concurrency_preserves_all_rows` | P0-1 并发写 + compact 压力，300 行不重不漏 |
| `test_postgresql_create_index_uses_identifier_not_fstring` / `rejects_invalid_order` | P0-3 |
| `test_cli_import_ignore_mode_reports_rows_written_none` | C-M1 |
| `test_cli_restore_replace_rejects_non_iosql_target` | P0-6 |

`tests/test_migration.py` 更新两个 mock 用例适配 P0-4 命名游标（FakeCursor 增加
`fetchmany/__enter__/itersize`，断言去掉 OFFSET、改为验证无 OFFSET 单语句流式）。

## 验证

- 全量回归：`386 passed, 16 skipped`（基线 345 passed, 16 skipped；新增 11 例
  regression + PLAN-055 UI 测试，零失败）。
- 改动 9 个模块 `import` 全部通过；`lsp_diagnostics` 无新增错误（Pyright 存量
  类型推断噪声与 HEAD 基线一致）。
- 连带验证：`test_ui.py::test_truncate_requires_matching_confirm` 由失败转通过
  （P0-9 的 `auto_commit` 动态化修复了 truncate 依赖的提交路径）。
- **P0-4 外部核实**：librarian 对照 psycopg2 C 源码（`cursor_type.c`）确认
  `withhold=True` + autocommit 可用（`autocommit && !withhold` 才报
  ProgrammingError）、`fetchmany(n)` 直接取 n 行（itersize 仅影响迭代器）、
  `with conn.cursor(...)` 退出保证 `CLOSE` 下发、唯一游标名避免同连接门户冲突。

## 检查记录（复核轮，2026-08-23）

- 逐项复核 P0-1~P0-9 + C-M1/C-M4 的 diff 与边界：锁覆盖（write_buffer 经
  write→do_insert_all 入锁；migrate/repartition 写目标表各自持锁，无死锁）、
  表名校验不误伤既有表名、DuplicateColumn 导入仍被其余路径使用、`offset`
  参数为死参但保留兼容、`--target` 与 `--replace-source` 互斥故最终移动恒同盘。
- 复核发现并修复一处边界：C-M4 备份 `os.replace` 在跨文件系统 `--backup`
  时抛 EXDEV（旧 copytree 支持跨盘）。初版补 `_move_dir` copy 回退保兼容；经
  权衡后改为 EXDEV 明确拒绝（跨盘无法原子备份，源库原封不动，报错退出），
  简化回滚分支、与原子化意图一致。

## 明确不改（本轮范围之外）

- P1/P2 全部留待 PLAN-B（性能）/ PLAN-C（一致性清理）后续轮次。
- PG file 分区死代码分支未删除（读写在 `__init__` 已拒绝，代码清理归 PLAN-C）。
- `_select_query` 的 `offset` 参数保留为兼容（无调用方）。
- UI `test_ui.py`/`iosql/ui/` 为 PLAN-055 未提交内容，不在本轮 review 范围。

## 已知限制

- P0-1 并发修复以"写路径与 compact 共用锁"串行化实现；吞吐上 compact 与写互斥
  是预期代价。文档级"单 writer"声明未做（写入 README 待 PLAN-C）。
- P0-4 命名游标依赖 psycopg2 `withhold=True` 语义；本地无 PG 认证环境，
  真实连接冒烟未执行（与 PLAN-053 T2 同限制），SQL 构造路径由 mock 背书。
- `_return_connection` 无条件 rollback 对 WAL 模式每连接归还多一次 no-op 调用，
  成本可忽略；对显式 `transaction()` 上下文无影响（事务内连接不入池）。