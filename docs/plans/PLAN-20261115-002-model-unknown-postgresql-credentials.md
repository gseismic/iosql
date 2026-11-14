# PostgreSQL 凭据环境变量与密码文件示例

创建时间：2026-11-15 01:44（Asia/Shanghai）\
模型：Codex（环境说明基于 GPT-6，未提供可确认的实际模型 ID）\
分析范围：WORKTREE，基准提交 `b2617de47ff9882bb50c48d52f91ead185cf1b24`。任务开始前已有未提交的示例整理改动：`README.md`、`examples/`、`skills/iosql-usage/`、`tests/test_example_scenarios.py` 和 `docs/plans/PLAN-20261115-001-*`，须保留并在其基础上更新；本计划及后续变更尚未固定为提交。

## 目标、范围与不处理事项

让 PostgreSQL 示例和使用说明展示不在 URI 中嵌入密码的连接方式；验证 PostgreSQL 官方 `PGPASSWORD` 和 `PGPASSFILE` 对 iosql 读写及迁移是否有效。更新仓库文档和 `iosql-usage` 技能及其示例镜像。保留已有带密码 URI 的兼容性，不自行引入 `!password` 等非标准解析语法，不改写用户配置或连接真实业务库。

## 当前状态、依据与约束

- `iosql/engines/postgresql/table.py`、`iosql/coordination_postgresql.py` 与 `iosql/migration_postgresql.py` 把 URI 交给 psycopg2；PostgreSQL libpq 支持未在连接串中指定时从 `PGUSER`、`PGPASSWORD` 或密码文件读取默认值。
- PostgreSQL 官方文档将 `PGPASSWORD` 列为可用选项，但提醒部分系统可能暴露进程环境变量；密码文件以 `PGPASSFILE` 指定，在 Unix 上需限制为仅所有者可访问。连接 URI 的 `user:!password@...` 按普通密码解析。
- 相关 README、技能参考和迁移 dry-run 示例中仍有带密码的演示 URI；这些是占位符而非真实凭据，但容易促使使用者把密码写入配置或日志。
- 项目此前的 PostgreSQL 示例测试使用 trust 测试库，不能证明密码认证；需用隔离的 SCRAM 认证实例实测。

## 实施步骤

1. 在独立 PostgreSQL 14 测试实例上验证无密码 URI + `PGUSER`/`PGPASSWORD`，以及指定 `PGPASSFILE` 的真实认证；同时验证 `!password` 不触发变量展开和显式密码优先级。
2. 保持库对外接口不变，将示例 URI 改为不包含密码的形式；更新根 README、示例说明和 `iosql-usage` 导航/参考，提供交互输入环境变量的最小流程与密码文件说明。
3. 同步根示例到技能镜像，增加必要的真实认证回归，覆盖直接读写与迁移；验证旧的显式密码 URI 仍可用，不输出测试密码。
4. 复核已有工作区改动、链接、技能格式和相关测试结果，生成结果文件并更新计划索引。

## 验证和完成标准

- SCRAM 认证测试中，基本 PostgreSQL 读写及跨后端迁移分别能够通过 `PGPASSWORD` 和 `PGPASSFILE` 成功连接；错误密码和 `!password` 字面值不能借助另一个环境变量意外认证成功；显式密码 URI 的既有行为不变。
- 文档明确 URI 中有密码时连接参数优先，说明 Unix 密码文件权限、环境变量的暴露边界，以及官方链接；不把环境变量描述成加密存储。示例运行指令可复制、无需将真实密码写入终端历史。
- 根示例与技能副本逐文件一致，技能验证及相关回归通过。未提交原有改动，不打印或保存真实用户凭据。

## 风险、回滚与实施前自查

密码来源由 PostgreSQL 驱动处理，不能扩展 URI 语法后改变已有字面密码的语义；真实认证只在临时 PostgreSQL 进程中使用临时生成的测试密码。密码文件和环境变量均不是加密存储，应解释各自边界。测试使用自建临时集群和临时密码文件，结束后清理自身目录。回滚仅撤销本次新增测试及文档中的凭据示例变更，保留本任务开始前的未提交改动。

实施前自查：方案使用现成 libpq 约定，无须修改公共 API；同时验证用户示例中建议的自定义写法及已有显式密码兼容。文档和技能镜像须使用相同的连接示例。
