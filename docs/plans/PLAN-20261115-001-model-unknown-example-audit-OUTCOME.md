# 示例场景梳理与 PostgreSQL 入门示例：结果

创建时间：2026-11-15 01:24（Asia/Shanghai）\
执行模型：Codex（环境说明基于 GPT-6，未提供可确认的实际模型 ID）\
结果范围：WORKTREE；基准提交 `b2617de47ff9882bb50c48d52f91ead185cf1b24`，尚未提交。相关改动包括 `README.md`、`examples/` 及其金融/mysqlite 子目录中的示例与说明、`skills/iosql-usage/` 中的镜像及引用、`tests/test_example_scenarios.py`、本计划及 `docs/plans/INDEX.md`。新增的两个示例及其镜像、测试和计划/结果文件仍为未跟踪文件。

## 完成情况

- 新增 `examples/01_quickstart_postgresql.py`：独立声明 PostgreSQL 表契约、写入并按标的和时间窗口查询；使用 `ignore` 跳过重复主键，保留示例表。新增 `examples/03_partial_upsert.py`，独立展示未提供字段的保留行为。
- 清点 22 个主示例、4 个金融示例及 9 个 mysqlite 示例；`examples/README.md`、各子目录索引、根 `README.md` 和 `skills/iosql-usage/SKILL.md` 按常用与按需场景组织。频率为基于项目场景的估计，没有访问统计。保留已有脚本路径，并解释 `table()` 与 `timeseries()` 等对照示例的不同动机。
- 将 `14_parquet_backend.py` 收敛为 Parquet 写入和重开查询；将 `15_postgresql_timeseries_query.py` 收敛为 PostgreSQL 查询计划与索引。跨后端迁移、最新截面和字段级更新分别由其他示例覆盖；同步修正相关文档。
- 移除共享的 `_example_support.py`，此前使用它的脚本改为自包含；金融和 mysqlite 示例的运行目录改为相对脚本自身，复制单个文件后无需原目录结构。需要新目标的布局和迁移示例使用独立运行目录，保留先前结果。
- PostgreSQL 迁移示例不再删除固定或指定的目标表；迁移前拒绝已有同名关系，默认使用随机名称，输出精简的行数与校验结果，不输出完整 DSN。`15` 统一从 `POSTGRES_DSN` 读取连接配置；启动脚本和说明同步调整。
- 已同步技能示例副本、导航与 Parquet/PostgreSQL 参考文档。原计划要求的单脚本独立运行验证在用户补充要求后纳入并完成。

## 验证

- 在每项测试独立的空目录中，仅复制当前脚本运行；35 个 Python 示例全部运行成功。PostgreSQL 基础读写和查询诊断使用隔离 PostgreSQL 14；两个迁移示例分别覆盖无连接 dry-run 和真实迁移。
- `tests/test_example_scenarios.py`：在隔离 PostgreSQL 14 上 **55 passed**。覆盖重复写入与重开、字段级更新、重复迁移保留上次结果、已有源库和目标库、PostgreSQL 表/空表/视图/分区保护、随机目标以及测试密码不出现在成功输出中。随后为 PostgreSQL 连接显式关闭作局部调整，对相关用例和镜像一致性再次运行：**11 passed，44 deselected**。
- 将现有示例回归测试及示例复制到临时目录执行：**8 passed**。未重置工作区内已有的运行时数据。
- 技能格式验证通过；主示例和技能副本逐文件一致；22 个主示例及 13 个子目录示例均被相应索引覆盖；修改后的入口文档本地链接检查通过，`bash -n examples/start_postgresql_test.sh` 与差异空白检查通过。

## 限制和遗留

本次运行环境为 Linux，`17_safe_migration_and_paths.py` 的 Windows 原机分支未验证；真实 PostgreSQL 验证使用版本 14。没有发现需要另行登记的未解决事项。未修改 `GOAL.md`，未执行 Git commit 或 push。
