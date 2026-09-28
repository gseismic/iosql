# PLAN-072 全库 Review 问题修复与多用户并发评估

- 日期：2026-09-29
- 基线：`d005420`
- 问题清单：`docs/review/code-review-20260929-full.md`（P0×3、P1×8、P2×4）
- 结果文件：`docs/dev/PLAN-072-full-review-fixes-and-concurrency-OUTCOME.md`
- 并发设计文件：`docs/design/multi-user-concurrency-20260929-overview.md`

## 目标与原则

1. 逐项关闭本轮 review 的 15 项缺陷，每项有能在旧代码失败的回归用例；风险行为优先在入口拒绝并给出修复指引，不允许静默漏行、错写或删库。
2. 保持现有高层 API 的常见调用路径；需改变边界行为时在 README/skill 说明。数据删除、恢复、覆盖的判断以真实库结构和磁盘状态为准。
3. 评估同进程多线程、同机多进程/多个 `Database` 实例、读写与布局维护并发的实际保证。设计文档写明可支持的场景、无法跨文件原子化的边界与后续演进路径；至少用确定性测试验证当前主要竞争风险。
4. 按 AGENTS.md 先更新计划、再改代码，实施后 review、修复问题、生成 OUTCOME，中文 commit 中记录 PLAN 与 OUTCOME。

## 实施批次

### A. 发布与破坏性操作（P0）

| 项 | 实施 | 验收 |
| --- | --- | --- |
| P0-1 | `pyproject.toml` 显式打包 `iosql/web/static/**`；加产物级测试/冒烟。 | wheel/sdist 隔离安装后首页及主要 JS/CSS 返回 200。 |
| P0-2 | 统一 `Table.rename` 与 `Database.rename_table` 的目标身份检查、暂存换入和失败回滚；直接调用需同步/失效 `Database` 缓存。 | 故障注入保住源、目标；普通目录拒绝；直接重命名后旧名不再路由新表。 |
| P0-3 | restore 对备份及覆盖目标均验证真实 iosql manifest；换入前校验复制后的副本，明确空库处理。 | 普通/伪造/损坏目录拒绝且目标字节保持；真实备份成功。 |

### B. 数据与跨引擎语义（P1）

| 项 | 实施 | 验收 |
| --- | --- | --- |
| P1-1 | 明确分区表主键作用域；在没有全局索引的布局下，对无法保证正确性的 upsert/replace 显式拒绝或实现可证明正确的全局处理。 | 跨分区同 pk 场景不再留下两行且不静默改变既有行。 |
| P1-2 | 与 P0-2 一起修直接重命名后的缓存映射；同一 owner 回调也覆盖 PG 直接重命名，并让 PG `Database.rename_table(overwrite=True)` 传递覆盖参数。 | `db.table(old)` 不再返回 `new`；PG mock 验证覆盖参数和缓存。 |
| P1-3 | SQLite/PG 数字文本横轴按数值求边界，异常大数显式报错；Web 接口同测。 | `2/10/3` 三点均进入桶；定宽日期不回归。 |
| P1-4 | candle 桶内按横轴值选开收盘，同值时定义稳定规则。 | 乱序写入仍返回最早 open、最晚 close。 |
| P1-5 | 无唯一排序键时不提供会静默漏行的 keyset 游标，明确返回可操作错误。 | 无 pk 同值场景不再返回看似完整的错误分页。 |
| P1-6 | PG `iter_rows` 改逐行；引入 `iter_batches` 给迁移使用。live PG 验证发现命名游标在首次 `fetchmany` 前 `description=None`，读取列名须推迟到首次 fetch 后。 | 两引擎同名 API 返回形状一致，迁移仍按批复制；真实 PG 与延迟描述 mock 均通过。 |
| P1-7 | SQLite `table_names` 仅识别真实表；读路径对普通目录无副作用。 | 普通子目录不在列表，Web/CLI 不当作表；真实表正常列出。 |
| P1-8 | PG 表枚举与引擎使用一致的 schema/search_path 规则。 | mock 验证 SQL，能用 live PG 时覆盖非 public schema。 |

### C. 输入、文档与发布质量（P2）

- P2-1：`force_partition` 在 `spec=` 路径不被默认 False 覆盖，显式冲突要报错。
- P2-2：Web `order_by` 全词法校验，非法值返回 422。
- P2-3：README 的 `select()` 默认返回形状与实现一致。
- P2-4：明确 `web --host` 的本机安全边界；参数与 Host 白名单可用性一致。
- 同步 `skills/iosql-usage` 相关说明，并对照报告保留风险更新操作建议。

### D. 多用户并发评估

1. 梳理 SQLite WAL、每文件单 writer、manifest 与数据分文件、进程内 `_compact_lock`、连接池预算、读游标快照、rename/restore/compact/repartition 的锁与生命周期。
2. 用两个独立 `Database` 实例及两个进程执行：同表并发 append、同分区/不同分区写、读写交错、segment 滚动与维护交错；记录行数、唯一性、manifest/segment 一致性、锁超时与读快照行为。
3. 若存在可局部修复的静默损坏或永久死锁，本计划内修复并加回归；跨文件事务/全局锁若需架构变化，在设计文档写明确边界与可执行后续方案，绝不宣称当前已有全库线性一致性。

## 验证门槛

- 每批改动运行对应测试；最终 `python -m pytest tests/ -q` 全量通过，无新增 skip。
- wheel 产物级隔离安装验证 Web；`python -m compileall -q iosql cli examples benchmarks`；非 vendor JS 语法检查；`git diff --check`。
- 针对破坏性操作和数据正确性跑故障注入/临时库用例；对 PG 没有 live DSN 的部分明确写出 mock 证据与缺口。
- OUTCOME 对 15 项逐条给出代码与测试证据，并附并发结论；未达成的项不得写成已修复。
