# 交接：PostgreSQL 修复已提交，下一步检查示例

创建时间：2026-11-15 17:11（Asia/Shanghai）\
模型：Codex（环境说明基于 GPT-6，未提供可核实的实际模型 ID）\
项目：`/Users/mac/pai-studio-fin/library/iosql`\
代码版本：`3cb4c8baf226bb53715f2cf8f4bc22aed77f00ad`，分支 `master`。该提交包含本轮 PostgreSQL 修复及验证记录；本文档和提交来源追加记录另行提交。未推送。

## 接手时先看

1. 先检查项目根 `GOAL.md`，再读 `AGENTS.md`。本次检查时 GOAL.md 不存在，不得自行创建。项目是支持 SQLite、Parquet 和 PostgreSQL 的库。
2. 当前交接以 `docs/HANDOFF.md` 为准；仓库根的 `HANDOFF.md` 是 2026-08-23 的历史内容，不能据其判断当前工作区或待办。
3. 用户最新要求是“先提交，不要压缩上下文，写交接文档，我一会新开”。本轮停止继续开发示例，保存现有修复与交接；新会话再接续下述工作。
4. 先运行 `git status --short --branch`、`git log -3 --oneline` 核对。任务外未跟踪的 `.playwright-cli/` 原本存在，未改动或提交，不要清理、暂存。

## 已完成的 PostgreSQL 工作

- 固定修复提交：`3cb4c8baf226bb53715f2cf8f4bc22aed77f00ad`，中文消息“修复 PostgreSQL 事务与分区逻辑并补充回归”；前一基准为 `620ee5e040159cb458c2fb60068e6352ee07b355`。
- 修复事务中断线后误用自动提交、delete 未提交、提交/回滚及读取流清理掩盖原错误、批内重复主键依赖 SQL 分页、字段级 upsert 覆盖顺序、实际写入计数、文本时间精度、并发布局保护、百分号标识符、超长名称静默截断串表。
- 公开 Table/Query 方法签名未改变。原五项及后续确认的同范围问题均完成修复与循环复审；这不代表证明绝对无错误。
- 计划：[docs/plans/PLAN-20261115-005-model-unknown-postgresql-correctness.md](plans/PLAN-20261115-005-model-unknown-postgresql-correctness.md)。
- 结果：[docs/plans/PLAN-20261115-005-model-unknown-postgresql-correctness-OUTCOME.md](plans/PLAN-20261115-005-model-unknown-postgresql-correctness-OUTCOME.md)。后者记录代码路径、准确行号、验证和兼容性边界。
- 主要实现为 `iosql/engines/postgresql/table.py`、`iosql/engines/postgresql/query.py`、`iosql/core/query/timeseries.py`；新增真实反例为 `tests/engines/postgresql/test_correctness.py`。

## 用户下一项要求：检查并修改示例

用户原话：“完成后还要检查例子，让例子符合规范 $library-examples-zh”。这是修改授权，不应只交付审查建议。**这项新示例检查尚未实际开始，示例文件未因本次新要求改动；不要把上一轮全量测试经过示例测试等同于已完成本轮规范审查。**

用户随后询问权限要求的含义，回答和约束见下一节。本轮只检查了 GOAL.md、确认 skill 路径存在，并准备交接，没有建立新示例计划或修改示例。

用户提供的 skill 路径（已确认存在）：

- `/Users/mac/pai-studio/skills/skills/library-examples-zh/SKILL.md`。
- 可用技能目录也有 `/Users/mac/.agents/skills/library-examples-zh/SKILL.md`。优先核对用户指定文件；本会话用户还提供了完整正文，用户要求优先于旧 skill 或旧文档。

该 skill 的关键要求：

- 每例尽量独立、单文件、极简但完整；只用公开接口和已声明依赖，不依赖其他例子的脚本、配置或产物，不靠改模块搜索路径才能运行。
- 文件开头写用途、执行目录、可直接使用的运行命令、依赖/环境/凭据/服务前置条件、预期结果及数据保留/清理方式。有实际前置条件的例子在自身文件内提供简短检查函数，主要逻辑前调用；无条件的例子不加空检查。
- 按真实任务保持正交，减少仅参数、数据量或等价调用不同的重复示例；不按 API 清单穷举，不把边界组合与大型测试脚本当日常示例。
- 组间和组内按预计频次排序，没有使用统计时注明定性判断。首组推荐 basic；已有项目约定可保留。分级编号方案是推荐参考，通常保留已有编号，通过索引安排阅读顺序，不为形式一致而大批改名。
- 单独运行每个目标例子，必要时复制到临时目录；验证前置条件缺失时的提示与提前退出。不能实际验证的服务或认证条件应明确说明。

接续入口：

- [examples/README.md](../examples/README.md)：当前按常用读写、按需维护、金融和低层工具导航，保留历史编号。
- `examples/`：主示例，包含 PostgreSQL、SQLite/Parquet、finance、mysqlite 等。
- `skills/iosql-usage/examples/`：示例完整镜像；修改主示例后同步，并逐文件检查一致性。
- `skills/iosql-usage/SKILL.md` 及 references、根 README 中的导航和链接可能需要同步。
- `tests/test_example_scenarios.py`、`tests/test_existing_database_examples.py`、`tests/test_postgresql_credentials.py` 提供复制到独立目录、保留数据、服务条件及密码来源等验证。
- 之前的示例工作见 `docs/plans/PLAN-20261115-001-model-unknown-example-audit.md` 至 `PLAN-20261115-004-model-unknown-example-review-fixes.md` 及各自 OUTCOME；其完成状态是历史依据，不代替按本次 skill 再检查。

建议顺序：读取用户指定 skill 和示例索引，梳理最小代表场景，检查现有前置函数、公开 API、依赖和重复；为多文件修改建立新计划（先查当天编号）；实施必要修改并同步镜像/索引；独立运行和复审，写对应 OUTCOME。不要顺手改 GOAL.md、无关核心逻辑或未经授权的数据库权限。

## 权限问题：不是当前测试账号授权不足

用户问：“分区写入现需表级 UPDATE、DELETE 或 TRUNCATE 权限之一，仅 INSERT 不够，什么意思，当前数据库给你的权限不够吗。”

已明确回答：**不是当前数据库授权不足。** 上轮使用本次自建的隔离 PostgreSQL 14.19 测试实例，测试账号有足够权限；没有访问或修改用户业务数据库授权。

这项角色要求是本轮布局锁修复引入的兼容性变化：原先 ROW EXCLUSIVE 锁无法阻止外部 ATTACH 穿过布局检查，现改用父表 SHARE UPDATE EXCLUSIVE，见 `iosql/engines/postgresql/table.py:859`。PostgreSQL 对此锁要求表级 UPDATE、DELETE 或 TRUNCATE 之一；仅 SELECT/INSERT 的非所有者写入账号会被明确拒绝。普通 SELECT 可继续，首次固定分区摘要仍需所有者权限。说明已同步到根 README 与仓库 skill references。

新建表的所有者通常已有所需权限；这条要求主要影响已有表上的低权限写入角色，**不是要求用户现在给当前测试账号补授权**。用户只是在询问含义，尚未要求撤销该修复、降级锁或给业务角色自动授权。之前助手说会重新评估兼容性代价，该评估尚未实施；需要时先比较能保留并发正确性的方案，不自行退回弱锁。

示例检查时应核对其权限说明与新行为是否一致，尤其是重开已有表、迁移与低权限角色场景；不要只写“能连接数据库”就声称具备分区写入权限。

## 验证和复现环境

- 最终工作区 PostgreSQL 专项：235 passed；最终隔离副本完整回归：1599 passed、99 skipped、0 failed、0 errors。实现、测试及使用说明共 11 个文件的 SHA-256 与最终工作区一致后才结束验证。
- ruff 和 git diff --check 通过。完整回归使用 Python 3.11.15、PostgreSQL 14.19、psycopg2 2.9.10、Arrow 1.3.0、DuckDB 1.5.4、pytest 8.4.2、FastAPI 0.121.0、Typer 0.12.5、Click 8.1.8。
- Typer 0.19.2/Click 8.3.0 下三项旧 CLI 测试在未修改 HEAD 也失败。记录为待评估 [TD-20261115-1621-001](todos/TD-20261115-1621-001-cli-test-dependency-compatibility.md)，没有借本轮修改 CLI 生产逻辑。
- 99 个跳过项包括缺少密码认证测试库、Playwright Chromium、Windows/特定文件系统条件和旧版废弃测试；未验证的场景不应说通过。
- 临时 PostgreSQL 实例已停止，临时数据库、验证副本及原始测试报告已清理；原路径 `/tmp/iosql-pg-fix.zzKzPN` 已不存在。没有本轮需等待的后台进程或待继续的 exec 会话。
- 本机 PostgreSQL 14 程序可用：`/usr/local/opt/postgresql@14/bin`。PATH 中 PostgreSQL 17 的 initdb 曾因缺少 `$libdir/dict_snowball` 初始化失败，不要反复盲试或复用失效的临时库。
- 如重新验证，创建新的独立实例与库。之前使用只监听临时 Unix socket 的实例；iosql URI 解析需显式 host/port，可用 `postgresql://iosql@localhost:5432/postgres?host=<新的临时目录>/socket`，由 query host 指向临时 socket。不要将测试连接指向业务库。
- 示例凭据回归需要真正的密码认证环境；无密码 trust 实例只能验证普通读写和迁移，不能证明 PGPASSWORD 被使用。

可参考的测试依赖命令（先配置独立测试库环境变量）：

```bash
uv run --no-project \
  --with 'psycopg2-binary==2.9.10' --with 'arrow==1.3.0' --with loguru \
  --with 'typer==0.12.5' --with 'click==8.1.8' --with 'pytest==8.4.2' \
  --with 'duckdb==1.5.4' --with 'fastapi==0.121.0' --with httpx --with uvicorn \
  python -m pytest -q tests --disable-warnings --tb=short -rs -p no:cacheprovider
```

POSTGRES_DSN 指定本轮测试库；需要真实运行 PostgreSQL 示例测试时，另设 IOSQL_EXAMPLE_TEST_POSTGRES_DSN。认证测试另需其文件头声明的 IOSQL_AUTH_TEST_POSTGRES_* 条件。全部环境变量要按实际前置检查配置，不能拿“未配置而跳过”当示例已运行。

## 提交和接续边界

用户已明确授权当前修复与交接的 commit，未授权 push。当前任务不继续示例修改；新会话依据用户要求接续示例规范检查。修改示例本身不自动授权新一轮 commit/push，需按新会话已有授权判断，不重复询问已明确获得的授权。

提交来源与修复验证详见计划、OUTCOME 和关联问题记录。本次不执行上下文压缩操作。
