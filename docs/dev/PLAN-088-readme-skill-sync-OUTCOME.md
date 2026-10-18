# PLAN-088 结果：README 与 iosql skill 同步

- 日期：2026-10-18（Asia/Shanghai）。
- 计划：[PLAN-088](PLAN-088-readme-skill-sync.md)。
- 状态：完成。

## 文档更新

- README：补齐 SQLite、Parquet、PostgreSQL 三后端能力对照，增加 13～17 示例链接与运行入口；说明 CLI 仅支持 SQLite/Parquet 迁移源，PostgreSQL 源使用 Python API。
- README：将并发说明更新为库级共享/独占租约，修正旧的流式排序说明、读取流生命周期、迁移前准备及已有代际库重命名的复制成本。
- README 与使用模式参考：迁移示例在同一上下文中完成预览与正式迁移，消除关闭源对象后继续使用的写法；补充目标空表、dry-run 不验证目标、临时目录位置及完整目标视图的空间成本。
- `iosql-usage` 入口：补充目标布局 `specs`、元数据维护和迁移异常的导航；将 `range.width` 从“每分区行数”纠正为“数值区间跨度”；更新 skill 展示说明，保留自动发现策略。
- API 参考：补齐 `description` / `column_descriptions` / `column_ranges` 参数及 `describe` / `update_metadata` 用法和后端差异；补齐迁移结果的 `rows_written`、`published`、`operation_id`、逐表源/目标契约与新增分区。
- 错误排查：补充 `DatabaseBusy`、`MigrationLockTimeout`、同线程读取流升级独占锁、恢复屏障、发布不确定、路径链接拒绝与 dry-run 边界；明确目标非空前置检查仍抛 `BadArgument`，转换后行数不匹配抛 `MigrationVerificationError`。
- CLI、Parquet 与设计契约参考：移除要求手工暂停全部协作写入的旧说明，补齐元数据命令、有序导出、源协调文件副作用；修正 PostgreSQL 关闭连接时回滚未提交事务的说明。

## 审阅与验证

- 对照 `iosql/database.py`、`iosql/migration.py`、`iosql/migration_postgresql.py`、`iosql/spec.py`、`iosql/exception.py`、三后端实现与统一迁移设计自审新增文案。
- 11 份变更 Markdown 文档中的 116 个本地链接及章节锚点检查通过。
- 仓库 `examples/` 的 39 个示例文件与 skill 副本逐文件一致（排除 runtime 与 Python 缓存）。本轮未修改示例。
- README、skill 入口与参考资料中 94 个 Python 代码块的 AST 语法检查通过；仅解析，不执行业务代码。
- skill `quick_validate.py` 输出 `Skill is valid!`；`agents/openai.yaml` 可解析，短说明为 54 字符，默认提示包含 `$iosql-usage`，自动发现仍开启。
- `git diff --check` 通过。本轮仅修改文档和 skill 展示信息，未运行业务代码测试，未新增后端或 Windows 运行验证。
