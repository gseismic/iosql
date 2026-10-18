# PLAN-083 迁移接口与安全发布验证

- 时间：2026-10-18 12:43（Asia/Shanghai，最终汇总）。
- 模型：Codex（GPT-6，精确模型 ID 未提供）。
- 版本：WORKTREE，基准 `7a0f87fd1289eb551896b9d38c509cd2c4edc311`。涉及本任务新增协调/发布模块、Database/Table/查询入口、CLI、分区单位处理和测试；不是基准提交的未修改代码。
- 环境：Linux，CPython 3.12.8，SQLite/Parquet 实际文件；本机独立 PostgreSQL 14.22，端口 55478，测试账号 iosql。没有连接生产库。

## 目的与范围

验证两个作用域共用 tables/specs、源布局不变、目标按新布局重新路由、先完整转换再发布、维护窗口互斥、异常解锁、旧缓存刷新和中断恢复。覆盖实际数据和跨文件系统，旧 SQL 生成单元测试显式替换协调/发布依赖，不在生产代码提供绕过锁的后备路径。

新增用例位于 `tests/test_safe_migration.py` 和 `tests/test_safe_migration_postgresql.py`：

- SQLite/Parquet 新分区、无分区、改分区列/种类、calendar 月份/时区、段布局、空表及重开后写入。
- 毫秒与秒单位转 calendar；早期 epoch 值不变、分区归属正确、重开后时间范围剪枝正确；PG BIGINT 日历边界和最终写入 batch_size。
- None/list/dict、单表作用域、重复列表、空选择、改名、旧 target_table/位置参数、非法覆盖字段、无效时区先于目标连接。
- 合并保留未选中表，ignore/upsert/replace 语义，统计真实新增分区；row-count 在非空目标前置失败，实际暂存与 PG 正式目标再次计数。
- 空闲 SQLite WAL 缓存重连与后续写入，已绑定 PG 选中/未选中旧对象保持可用。
- 迁移禁止已有对象读写和查询消费、流完整持锁、同线程拒绝升级、跨线程超时、fork 不解除父进程文件/PG advisory 锁。
- READY/COMMIT_DECIDED/COMMITTED 处 `os._exit(73)` 直接退出，写入先恢复、dry-run 不恢复、控制损坏拒绝访问。
- 读取迭代器中途失败关闭，失败暂存保留，清理在最外层租约释放后；跨 `/dev/shm` 到 `/tmp` 的真实复制。
- 代际后的备份/恢复/重命名，CLI --specs、直接扫描持租约、恢复时目标并发创建后重新检查覆盖授权。
- 真实 PG 源专用快照、目标表锁阻止直接 SELECT、保留已有 OID/视图、所有表最终事务回滚、提交回复丢失时根据持久状态判断，原调用方未提交事务不被迁移修改。

## 执行方式

完整回归：

```bash
POSTGRES_DSN='postgresql://iosql@127.0.0.1:55478/postgres' \
uv run --no-sync --offline --with pytest --with psycopg2-binary --with duckdb \
  --with fastapi --with httpx --with uvicorn --with 'pip==25.2' \
  --with wheel --with setuptools \
  python -m pytest tests -q --tb=short
```

pip/wheel/setuptools 仅供仓库既有发布产物测试调用，未修改项目运行依赖。首次获取 pip 最新包遇到下载超时，改用验证环境的固定 pip 25.2 后继续；这些环境准备失败没有记为测试通过。

基线对照：用 `git archive 7a0f87fd1289eb551896b9d38c509cd2c4edc311` 建立独立临时副本，进程切换工作目录并把该副本放在 sys.path 首位，输出 iosql.__file__ 确认使用基线代码，再运行以下六个失败及发布产物测试。

## 已取得结果

- 最终新增安全迁移组：**52 passed**，包括实际 PostgreSQL、缓冲和任务历史；[独立原始记录](safe-final.txt)。最后源恢复错误定位断言也已纳入完整回归。较早 45 项记录保留为 [阶段记录](safe-migration.txt)。
- 最后 CLI/本地相关运行：104 passed；[CLI/本地记录](cli-and-local.txt)。PG 已绑定对象的恢复由上一组覆盖。
- 最终完整回归：**880 passed、6 failed、15 skipped**，122.61 秒；[完整原始记录](full-regression.txt)。六个失败均在基线复现；最终记录包含 CLI、缓冲、历史归档、父目录持久化及 PG 超时配置保留修订。
- 包含缓冲写入、提交历史归档、目录 fsync、维护与原始示例的针对性组：176 passed。[针对性原始记录](accepted-targeted.txt)。
- 原始基线：6 failed、1 passed；[基线对照](baseline-comparison.txt)。失败为三个 mysqlite 旧测试与三个 Web JSON/流输出旧测试，发布产物测试通过。没有以新增 skip 或屏蔽断言规避这些失败。
- 修改的 40 个 Python 文件 Ruff 通过，git diff --check 通过；8 组示例/skill 副本逐字节一致；[静态检查](static-check.txt)。
- 示例 11/12 在独立 PG 上完成实际迁移/往返；示例 16 连续运行两次成功，skill 副本也成功：[11](example11.txt)、[12](example12.txt)、[16 重复执行](example16-repeat.txt)、[16 副本](example16-mirror.txt)。

一次中间回归与示例误用同一个 PG 数据库并行执行，出现预期的 DatabaseBusy。已纠正测试调度，完整回归与示例顺序执行；该次互相干扰的运行未作为通过证据。

提交检查时仅移除了完整回归和基线对照日志的行末空白，异常、断言和结果统计均保留。

## 基线仍失败的六项

1. `test_mysqlite_integrity_error_rolls_back_connection`：旧测试在连接池上下文显式 commit=False，当前基线已拒绝这种丢失提交的用法。
2. `test_group_rows_by_keys_groups_heterogeneous_rows`：列顺序不同的字典分组与旧断言不一致。
3. `test_auto_commit_false_requires_transaction_or_explicit_commit`：旧测试的预期异常未发生。
4. `test_numeric_text_buckets_and_overflow`：响应按流输出，旧测试直接 response.json() 得到 Extra data。
5. `test_web_candle_order_pagination_and_sort_validation`：同上。
6. `TestReadOnlyApi.test_chart_line`：同上。

这些事项与本次迁移功能无关，保留原有实现与断言，在结果中如实报告。15 个原有 skip 不算通过；新增安全迁移用例在本机独立 PG 配置下没有 skip。

## 验证边界

本地保证适用于同机、接入协调协议的 iosql 进程。没有把原始文件写入、旧版本客户端、NFS/分布式锁、Windows 或真实断电测试描述为已验证。终止场景使用不执行 Python finally/atexit 的进程直接退出；PG 的提交应答丢失通过真实提交前/后抛 OperationalError 注入。

最终源码校验和：[source-hashes.txt](source-hashes.txt)。本任务代码未在最终完整回归启动后修改；随后仅更新设计、计划、结果和测试说明。

最终审计：完整回归 880 通过、6 个已复现的基线失败、15 个原有 skip；新增 52 项安全迁移均通过，最后静态检查通过。无待执行验证。本次未提交 Git。
