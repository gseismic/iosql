# PLAN-093 全库 Review 修复结果

- 日期：2026-10-23。
- 审查基线：`c329cd07958f890b483b78e8603880f86d62aab6`。
- 依据：[原始 Review](../review/code-review-20261022-full.md)、[实施计划](PLAN-093-full-review-fixes.md)、[设计](../design/review-fixes-20261023-overview.md)。
- 结果：R01–R17、3 项未解决已知问题、6 项基线失败及 15 项静态检查问题全部处理。新增 **90 项**针对结果、故障与成本的回归；最终全套 **1111 passed、19 skipped、0 failed**。

## 逐项交付与证据

下表测试位于 `tests/test_plan093_review.py`、`tests/test_plan093_resources.py`；最终测试结果逐用例保存在 [pytest.xml](../tests/20261023-PLAN-093/pytest.xml)。

| 项 | 最终行为 | 当前证据 |
| --- | --- | --- |
| R01 | 单表覆盖重分区先复制完整快照，仅重建选中表；拒绝覆盖未选中表名；独占窗口重新检查冲突 | `test_repartition_replace_preserves_unselected_tables`：其他表内容、完整契约、真实索引及 dry-run 范围均保留 |
| R02 | compact 构建代不可见；一次元数据事务切换新旧集合和发布记录；外部排序不物化整个分区 | `test_compact_generation_failure_is_invisible`、`test_compact_process_exit_preserves_previous_generation`：第二个 chunk、发布失败、进程退出后旧代仍可读，重开/重试成功；`test_validate_checks_committed_generation_membership` 检出缺成员 |
| R03 | 输入类型与持久类型不能证明一致时禁用相应统计裁剪，兼容旧 text 列错误统计；变量谓词不使用常量范围裁剪 | `test_text_affinity_stats_match_native_sql`：比较、IN、BETWEEN、NULL、混合输入以物理 SQLite SQL 为对照 |
| R04 | write 只解析一次 mode，显式 append/ignore/replace/upsert 优先于默认 conflict | `test_explicit_mode_overrides_default_conflict`：SQLite/Parquet 完整交叉矩阵；verification.py 另验证 4 种迁移目标默认策略下显式 append 失败且源/目标不变 |
| R05 | 命名空间 revision 持久失效；缓存目标重新绑定新数据，已移除源对象关闭旧连接并报错 | 两实例测试及 `test_rename_invalidates_warmed_handles_in_another_process`：SQLite/Parquet 子进程在发布前预热源、目标句柄 |
| R06 | 预留前固定后端元数据；DuckDB/SQLite 真实扫描计入预算；文件池、进程配额和执行继承查询截止时间 | `test_parquet_ordered_stream_at_budget_limit`、资源关闭/超时及池等待测试；既有连接预算、取消和跨线程清理测试均通过 |
| R07 | 局部主键布局的 Web 游标加入物理文件身份及布局/排序版本，完整保留重复行 | `test_web_pages_preserve_all_physical_duplicates`：两后端、segment/普通分区、升/降序完整遍历 |
| R08 | strict 在任何业务文件写入前校验整批行形态和字段，包括 extra_data | 严格表首次写、新分区、新 segment 及坏行拒绝测试；拒绝后行数、分区和契约不变 |
| R09 | 无序 LIMIT 按剩余额度逐文件下推；OR 在文件内用同一 SQL 求并集 | `test_unordered_limit_only_reads_requested_rows` 及 verification.json：limit=1 物理只读 1 行 |
| R10 | 无序分批读取使用专用只读连接、单游标 fetchmany，迁移/导出复用 | `test_unordered_batches_use_one_linear_cursor`：2 万行、每批 100，仅一条读取连接、约 6 万条 VM 指令；关闭与超时归还预算 |
| R11 | segment ignore 按持久主键类型批量查重、保留批内首值，新增行合并写入并增量统计 | `test_segment_ignore_writes_batches`；20 行从 20 次写入/20 次全列统计变为 1 次写入/0 次全列统计 |
| R12 | 首页告警仅看持久行数；计数及 stats 对 legacy 仅 COUNT，segment 使用 manifest；不为数量获取列范围 | `test_web_lightweight_stats_never_scan_column_bounds`：将全列统计函数替换为会抛错的探针，首页、count、stats、列表均正常 |
| R13 | 最多缓存 8 个已解锁控制会话；每次调用仍取锁、读 generation；只缓存控制表“已存在” | `test_pg_controls_reuse_reconnect_fork_and_close` 在 PG14.22 上验证 SQL/连接数、服务端断连、fork 与关闭；所有真实 PG 迁移互斥/恢复测试通过。取锁与首次目录检查使用不同语句快照 |
| R14 | Parquet append/ignore 仅输出新增数据片，经原子清单发布；修改/删除切换替换集合；备份、重开、统计、清理识别数据片 | `test_parquet_append_writes_only_new_rows_and_recovers_failed_publish`：五批每批 100 行，总输出 500 行；清单失败后旧数据不变，重开重试正确；既有往返迁移、schema 演进及清理测试通过 |
| R15 | latest 在各文件内以窗口查询筛每组候选；compact 临时磁盘排序；upsert 只查询命中主键并按分区直接定位 | `test_latest_compact_and_upsert_avoid_full_python_materialization` 禁用旧全表 helper 仍正确；更新 1 键只读 1 行，缺字段保留、显式 NULL 及重复输入沿用契约；3 项大数据测试全部通过 |
| R16 | SQLite 索引定义持久化，新分区、新 segment、compact、SQLite 目标迁移统一继承；drop 清理定义 | `test_logical_index_survives_new_files_compact_and_migration`：重开、PRAGMA、EXPLAIN QUERY PLAN、迁移及删除后未来文件验证 |
| R17 | 投影先校验逻辑列，物理列使用限定标识符，禁止 DQS 静默字符串；缺物理列报库级异常 | select/query/iter、空声明表、limit=0 分支校验；`test_missing_physical_column_is_an_iosql_error` 覆盖外部删除物理列 |

## 已知问题与旧测试

- KI-20261021-1806-002：稳定 schema 的低层批写不做无用途的值类型扫描；建表/补列才推断。连续同构行使用 itemgetter，保留异构输入顺序与表达式默认值。
- KI-20261021-2357-001：对象缓存稳定 Resource 身份并复用上下文 lease；1,000 次逐行 append 加 flush 的路径重解析为 0 次，迁移屏障保留。
- KI-20261021-1930-001：弱引用映射先复制，再建立强引用快照；确定性的重入注销与四线程 GC 生命周期测试通过。
- 6 项旧失败：完整性错误测试改在显式事务内验证回滚；异构分组断言按连续输入顺序；commit=False 在显式事务内允许、池外拒绝；3 项图表断言改按 PLAN-074 的原始点 NDJSON 契约验证。修复数字文本图表的数字排序，保留全部原始点，不恢复旧分桶行为。
- 额外修复依赖机器速度的 1ms 计数测试：明确注入延迟验证截止时间，避免小表优化后偶发失效。
- ruff 的 15 项问题全部修复，示例修改同步 skill 副本；公共导出使用显式重导出保留。

## 最终验证

证据目录：[20261023-PLAN-093](../tests/20261023-PLAN-093/README.md)。

- 完整 pytest：**1111 passed、19 skipped、128 warnings，141.21 秒**；包含真实 PG、200K 行、100K 行及 10K 分区 upsert 用例。
- 19 项跳过逐条列在日志：13 项废弃接口测试、6 项需要 Windows 原机的文件共享/大小写/junction 测试。跳过不算通过。
- ruff：`iosql cli examples benchmarks`、新增回归和验证脚本通过。
- 97 个 Python 文件以 3.9 语法规则解析；8 个自有 JS 文件 node --check 通过；40 个示例副本逐文件一致。
- `uv build --wheel` 成功；完整 pytest 中的 wheel 隔离安装、原生包导入和静态资源 HTTP 冒烟通过。
- 自审后的源码、文档增量空白检查通过；暂存检查仅排除原始审查终端日志 `docs/review/20261022/pytest-output.txt` 的历史尾随空格，原始文件不改写。执行计数和 4 项迁移显式 append 检查见 [verification.json](../tests/20261023-PLAN-093/verification.json)。

## 保留的成本与验证边界

Parquet 追加不重写历史完整行，主键检查仍可能扫描旧键列，清单开销随数据片数增长；修改和删除仍可重写命中的物理集合。latest 内存随分组数增长；compact 保留目标 chunk 与 segment 发布元数据，并使用与分区规模有关的临时磁盘。跨文件普通写入仍不提供崩溃原子性。

本次未访问用户业务库，未重新测量业务 HDD 吞吐，未进行 Windows 原机或断电实验。旧 profile 中的 commit/close 开销仍是历史观测，不据 CPU/调用计数推导业务总耗时倍数。索引迁移的文件继承验证针对 SQLite 目标，Parquet 继续不支持持久 SQL 索引。

## 提交范围

提交本次实现、回归、设计、计划/结果、相关 Review 和已知问题证据。开始任务前存在的 PLAN-092、20261021 性能回放记录及其索引更新保留在工作区，不纳入本次实现提交。最终提交消息逐条列出变更并引用 PLAN 与 OUTCOME 路径。
