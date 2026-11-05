# URI、迁移和 Parquet 绘图 Review 修复结果

完成时间：2026-11-06 05:49（Asia/Shanghai）\
执行模型：Codex（环境未提供可确认的实际模型 ID）\
实现范围：WORKTREE，基准提交 `c81d80f78de0168d6400e291e46e7d5581d09b02`；本次修改叠加在未提交的 URI 自动识别实现上，包括 `iosql/database.py`、`iosql/migration.py`、`iosql/utils/uri.py`、`iosql/utils/storage_identity.py`、`iosql/cli.py`、`iosql/web/app.py`、测试和文档，尚未固定为提交。

## 完成情况

1. 已缓存表的访问不再请求库级独占类型锁，仍检查类型标记、表路径和元数据符号链接；不同表经 `db.table()` 调用可以并行写入。
2. 迁移目标自动识别和源表预检所需的租约、类型锁共用 `lock_timeout` 预算。CLI 从源库识别开始计算剩余预算；等待超时返回 `MigrationLockTimeout`。预览行数统计不持有库级独占类型锁。
3. 有有效 SQLite 类型标记的空库可以备份、恢复和作为覆盖目标；未标记空目录、类型不匹配及损坏的标记仍被拒绝。
4. `Database.create` 在持久化库标记前验证构造参数，参数错误后可直接修正并重试。
5. Parquet 兼容图表接口使用后端支持的查询校验数字文本轴，并在响应开始前拒绝非数字文本和超出 64 位范围的值；筛选条件与分区裁剪继续生效。
6. 兼容图表的文本轴使用 64 位整数转换，SQLite 与 Parquet 均能按数值顺序输出毫秒时间戳和数字日期。

## 验证结果

- 新增 `tests/test_uri_review_fixes.py`：29 项通过，覆盖六项 Review、两类目标锁分别等待和超时、CLI 源识别、共享预算、预览并发、空目录保护、Parquet 筛选和文本溢出。
- 使用隔离 PostgreSQL 18 运行完整测试：`1523 passed, 19 skipped, 144 warnings in 234.16s`；包含浏览器测试。
- Ruff F 类检查、Python 3.9 语法解析和 `git diff --check` 通过。全部修复测试使用隔离目录，未修改用户业务数据。

## 计划偏差与遗留

自查发现源库自动识别和源表预检也存在同类超时预算缺口，已先更新计划并一并修复。缓存路径增加了符号链接检查，避免为提高并发而降低原有路径保护。测试在 Linux 完成，Windows 原机文件系统未实测。

工作区变更尚未提交。用户原有 Web 进程仍在运行，需在其原终端停止并重启后加载这些修复；本次未终止该进程。
