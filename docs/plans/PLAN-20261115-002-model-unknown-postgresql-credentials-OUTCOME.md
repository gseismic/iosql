# PostgreSQL 凭据环境变量与密码文件示例：结果

创建时间：2026-11-15 01:51（Asia/Shanghai）\
执行模型：Codex（环境说明基于 GPT-6，未提供可确认的实际模型 ID）\
结果范围：WORKTREE；基准提交 `b2617de47ff9882bb50c48d52f91ead185cf1b24`，尚未提交。本次涉及 `README.md`、`examples/01_quickstart_postgresql.py`、`examples/11_sqlite_to_postgresql_migration.py`、`examples/12_sqlite_postgresql_roundtrip.py`、`examples/README.md`、`skills/iosql-usage/SKILL.md`、对应技能示例副本及 `skills/iosql-usage/references/{api-reference,troubleshooting,usage-patterns}.md`，新增 `tests/test_postgresql_credentials.py` 和本计划/结果，并更新 `docs/plans/INDEX.md`。上述部分文件包含任务开始前的示例整理改动，已保留。

## 完成情况

- iosql 原有 PostgreSQL 连接已经通过 psycopg2/libpq 读取标准凭据来源，无须修改数据库引擎或公开 API。`PGPASSWORD`、`PGUSER` 和 `PGPASSFILE` 可配合不含密码的 URI 使用；原有显式密码 URI 继续有效。
- 主 README、PostgreSQL 示例说明和技能导航/参考均改用不含密码的连接示例。文档给出交互式输入 `PGPASSWORD`、已有变量映射和密码文件用法，说明优先级、Unix 权限、明文存储边界以及 PostgreSQL 官方资料。
- `!password` 不增加自定义解释：它按字面密码传给 PostgreSQL，显式密码优先于环境变量。PostgreSQL 迁移示例的 dry-run 占位 URI 和技能镜像同步去除密码占位符。

## 验证

- 在临时 PostgreSQL 14 实例启用 SCRAM 密码认证，使用包含冒号、`@` 和反斜杠的临时测试密码，分别验证环境变量和 `PGPASSFILE` 的基本读写、查询诊断和双向迁移；均成功，未输出测试密码。
- 显式错误密码 `!password` 搭配正确 `PGPASSWORD` 时认证失败；URL 编码后的正确显式密码搭配错误 `PGPASSWORD` 时成功。错误 `PGPASSWORD` 优先于正确密码文件；清除后密码文件成功。Unix 下 `0644` 密码文件被拒绝，改为 `0600` 后成功。
- `tests/test_postgresql_credentials.py` 与示例场景回归在 SCRAM 实例上共 **61 passed**；补充密码文件权限测试后，对凭据和示例镜像再次验证：**8 passed**。技能格式检查、11 个文档的本地链接检查和 `git diff --check` 通过。

## 限制与遗留

真实密码认证在 Linux 和 PostgreSQL 14 上验证；Windows 密码文件权限处理以 PostgreSQL 官方文档为依据，未做 Windows 原机验证。未改动用户数据库或实际凭据，未提交或推送。
