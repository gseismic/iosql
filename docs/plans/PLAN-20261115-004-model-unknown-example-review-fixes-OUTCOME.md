# 修复全部示例检查问题：结果

创建时间：2026-11-15 02:57（Asia/Shanghai）\
执行模型：Codex（环境说明基于 GPT-6，未提供可确认的实际模型 ID）\
结果范围：WORKTREE；基准提交 `b2617de47ff9882bb50c48d52f91ead185cf1b24`。`README.md`、`examples/`、`skills/iosql-usage/`、相关测试和计划索引包含前序及本次未提交内容，结果尚未固定为提交。

## 完成情况

1. 原先清理固定目录的本地示例改为每次创建独立运行目录；部分更新和 Parquet 入门也使用独立目录。旧数据和前次运行数据不会因再次运行被删除。目录库备份例使用自动清理的临时目录。
2. SQLite → PostgreSQL 的两个迁移示例通过 PostgreSQL `CREATE TABLE` 原子占用与源表契约兼容的目标表名，已有或并发创建的同名关系会被拒绝。迁移失败可能留下已预留的表，说明中提示检查。
3. 大结果集示例逐行消费并只保留上一条时间和计数，读取流用 `closing()` 释放。doctor 示例显示标准错误和失败退出码，同时保留退出码 2 的诊断输出。
4. 环境变量密码示例要求 `PGPASSWORD`、不含密码的 URI，拒绝 `PGSERVICE` 和 URI `service` 参数；文档说明需使用密码认证测试库。PostgreSQL 示例在主要逻辑前检查驱动、连接和服务器版本；驱动安装提示不依赖源码目录。
5. 不再让备份示例假设文件系统区分大小写；原有大小写路径测试遇到不支持大小写区分的文件系统时在写库前跳过。路径、快照及锁防护继续由对应测试覆盖。
6. 36 个 Python 示例文件开头均说明用途、本文件所在目录的运行命令、前置条件、预期结果和相关数据去向。已有库示例在使用外部源时先检查表存在；SQLite 删列示例检查版本。
7. 日期表重复示例调整为跨自然日的交易日/session 查询；综合安全测试示例调整为单一的公开 CLI 备份场景。保留既有文件路径并同步索引、README 和 `iosql-usage` skill。
8. 主示例和 skill 示例 42 个文件逐项一致；skill 校验与文档相对链接检查通过。未修改库公开 API，也未提交或推送。

## 验证

- 在独立的 PostgreSQL 14 SCRAM 测试实例中运行 `tests/test_example_scenarios.py`、`tests/test_postgresql_credentials.py`、`tests/test_windows_paths_and_snapshots.py`、`tests/test_windows_lock_file.py`：**154 passed，6 skipped**。跳过项均为需要 Windows 原机能力的测试。
- 上述测试逐个复制并运行 36 个 Python 示例，并覆盖既有运行目录和前次数据保留、两个迁移示例并发预留同名表、service 密码干扰、密码认证、缺少驱动/服务及低版本服务、doctor 退出状态、十万行流式消费的内存上限和跨日交易日查询。
- 在临时复制的示例目录运行 `tests/test_existing_database_examples.py`：8 passed；交易日/session 和 CLI 退出码相关的既有测试各 1 passed。补充模拟大小写不敏感目录，确认路径测试在写库前跳过。
- `skill-creator` 的 `quick_validate.py`、示例镜像逐文件比较、文档相对链接检查、全部示例语法解析及 `git diff --check` 均通过。

## 偏差与限制

按实际前置条件补充了外部已有库缺表检查和 PostgreSQL 版本检查，并调整原有路径测试在大小写不敏感文件系统上的跳过条件，均属于本次检查修复范围。本机仅有 Linux；macOS 和 Windows 原机行为未实测，相关 Windows 原机用例如上跳过。每次运行保留的本地示例目录会逐渐占用空间，已在示例开头和索引中说明。
