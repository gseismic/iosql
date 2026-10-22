# iosql 全代码库设计与性能 Review（2026-10-22）

> 后续状态（2026-10-23）：全部问题由 [PLAN-093](../dev/PLAN-093-full-review-fixes.md) 修复，逐项证据与边界见 [实施结果](../dev/PLAN-093-full-review-fixes-OUTCOME.md)。下文保留原始审查基线与观测，不作为修复后结果。

- 审查基线：`c329cd07958f890b483b78e8603880f86d62aab6`，生产源码与该提交一致。
- 审查方式：按模块阅读源码及契约，沿查询、写入、迁移、资源管理路径交叉核对，并在临时目录执行复现和回归。
- 本次只新增审查报告和证据文件，未修复生产代码、未操作业务数据库、未提交 Git。开始审查前已有的 `docs/KNOWN-ISSUES.md`、`docs/dev/INDEX.md`、已知问题与 PLAN-092 等工作区变更予以保留。
- 参考：[上轮全库报告](code-review-20260929-full.md)、[已知问题索引](../KNOWN-ISSUES.md)、iosql 使用契约和 API 设计审查技能。

## 结论

**存在需要优先处理的数据安全、结果正确性和性能问题。建议先修复 R01–R07，再扩大数据量或把相关能力用于无人值守任务。**

本报告列出 **17 项问题：P0 1 项、P1 6 项、P2 10 项**。其中 15 项有本次临时库运行证据，R13 为 mock 调用计数，R15 为源码确认的既有限制。已有问题另外列出，不重复计入 17 项，也不把“已记录”当成“已修复”。

最严重的风险是：单表重分区会删除同库其他表；compact 中途失败会产生重复可见数据，深度校验仍然通过；segment 统计的类型语义与实际存储不一致，会把符合条件的数据裁掉。性能方面，若干名为限量查询、流式读取、批量写入和快速计数的接口，内部仍然执行全量读取、重复扫描或逐行写入。

优先级含义：P0 为可能造成无关数据丢失的破坏性操作；P1 为静默错数据、错误写入或持续阻塞；P2 为需排期处理的性能、契约和维护性问题。优先级不代表每个用户都会触发。

| 编号 | 级别 | 问题 | 证据类型 |
| --- | --- | --- | --- |
| R01 | P0 | 单表 `repartition --replace-source` 替换整个库，其他表消失 | CLI 实测 |
| R02 | P1 | compact 分批发布，中途失败后新旧 segment 同时可见 | 故障注入实测 |
| R03 | P1 | SQLite segment 统计使用写入前类型，错误裁剪真实数据 | 原生 SQL 对照 |
| R04 | P1 | 显式 `mode='append'` 被表的默认 conflict 覆盖 | SQLite、Parquet 实测 |
| R05 | P1 | 覆盖重命名后其他已打开实例继续读取旧目标表 | 两个 Database 实例实测 |
| R06 | P1 | Parquet 有序流读耗尽连接预算后自锁，查询超时无效 | 子进程超时与线程栈 |
| R07 | P1 | Web 游标把局部主键当作全局唯一键，跨 segment 翻页漏行 | TestClient 实测 |
| R08 | P2 | strict schema 在创建新分区时被绕过 | 临时库实测 |
| R09 | P2 | 无排序的 `select(limit=1)` 仍读取全部匹配行 | SQL 与读行数计数 |
| R10 | P2 | SQLite 无序分批读取反复 OFFSET，扫描工作量接近平方增长 | SQLite VM 指令计数 |
| R11 | P2 | segment `ignore` 批写退化为逐行查重、写入、统计 | 调用计数 |
| R12 | P2 | Web 快速计数及首页请求仍触发 legacy 全列统计 | 调用计数 |
| R13 | P2 | PostgreSQL 独立 API 调用反复新建控制连接和执行锁 SQL | mock 计数，未测网络延迟 |
| R14 | P2 | Parquet 小批追加反复重写整个文件 | 旧行重载计数 |
| R15 | P2 | latest、compact、低层 upsert_all 存在全量物化边界 | 源码确认，既有限制 |
| R16 | P2 | create_index 只影响当前文件，新分区不继承索引 | PRAGMA 对照 |
| R17 | P2 | SQLite 查询不存在的列，返回字符串而非报错 | 临时库实测 |

## 范围与验证边界

覆盖公共入口、契约、分区与条件表达式、SQLite/Parquet/PostgreSQL 引擎、mysqlite、迁移与锁协调、CLI、Web 后端与自有前端、打包、示例和 benchmark 入口。`iosql/` 包含 55 个 Python 文件、21,839 行；加上 CLI 包装、示例和 benchmark，本次语法解析覆盖 97 个 Python 文件。

这是全库范围的模块审查，重点深入了数据和资源生命周期的关键路径；不是每条分支均经过运行验证，也不是性能容量认证。未审计第三方 vendor 库内部，未运行真实浏览器交互、live PostgreSQL、Windows 原机、断电恢复或生产规模压测。3 个 `TestLargeData` 用例明确未运行。

| 验证 | 结果 | 限制与解释 |
| --- | --- | --- |
| `pytest -q tests -k 'not TestLargeData'` | **945 passed、6 failed、86 skipped、3 deselected**，48.84 秒 | 122 warnings；跳过不计为通过，包含 PostgreSQL/平台相关覆盖缺口 |
| 15 个专项探针 | 均得到报告所述结果，无探针框架异常 | 每项在临时目录运行；Parquet 阻塞案例在独立进程中超时终止 |
| Python AST 解析 | 97 个文件通过 | 仅证明当前解释器可解析 |
| 自有前端 JS `node --check` | 8 个文件通过 | 未验证浏览器行为 |
| `ruff check iosql cli examples benchmarks --output-format concise` | 15 项问题 | 主要为未使用导入、旧式 lambda 和变量命名；未自动修复 |
| `uv build --wheel` | 成功 | 不等于全部运行功能正确 |
| wheel 隔离导入与 Web 资源冒烟 | 12 个静态资源；`/`、`/style.css`、`/js/tab-charts.js` 均 200 | wheel 内生产 Python 内容与工作区一致；上轮“漏打包静态资源”问题本次未重现 |

环境为 Linux、Python 3.12.8、SQLite 3.46.0、DuckDB 1.3.2。环境记录见 [validation.json](20261022/validation.json)，完整回归输出见 [pytest-output.txt](20261022/pytest-output.txt)，静态检查见 [ruff-output.txt](20261022/ruff-output.txt)。

6 项失败与 [2026-10-21 扩展回归记录](../tests/20261021-18-WORKTREE/README.md) 中保留的失败相同：

| 用例 | 当前失败表现 |
| --- | --- |
| `test_plan056_fixes.py::test_mysqlite_integrity_error_rolls_back_connection` | 旧测试使用 `commit=False`，当前路径提前拒绝，未走到原测试预期的完整性错误 |
| `test_plan062_mysqlite.py::test_group_rows_by_keys_groups_heterogeneous_rows` | 分组结果与旧测试对异构字段顺序的断言不同 |
| `test_plan062_mysqlite.py::test_auto_commit_false_requires_transaction_or_explicit_commit` | 预期抛出的异常未出现 |
| `test_plan072_fixes.py::test_numeric_text_buckets_and_overflow` | 把 NDJSON 响应作为单个 JSON 解码 |
| `test_plan072_fixes.py::test_web_candle_order_pagination_and_sort_validation` | 把 NDJSON 响应作为单个 JSON 解码 |
| `test_web.py::TestReadOnlyApi::test_chart_line` | 把 NDJSON 响应作为单个 JSON 解码 |

这些失败需要按现行契约逐项处理，不能据此宣称“全量测试通过”，也不能直接把断言改到通过而跳过契约确认。本次未修改这些测试或实现。

## 数据安全与正确性问题

### R01 · P0：单表覆盖重分区删除其他表

- **位置**：[cli.py](../../iosql/cli.py) 第 1895–1913 行，先对选中表调用 `t.repartition_to()`，随后 `publish_snapshot(final_target_uri, source_uri)` 发布整个数据库。
- **触发**：源库有 `selected` 和 `keep` 两张表，执行 `repartition -t selected --partition 'range(id,width=20)' --replace-source --no-backup --yes`。
- **实测**：命令退出码为 0；操作前表名为 `['keep', 'selected']`，操作后仅有 `['selected']`。见探针 `cli_replace_source`。
- **影响**：单表选择器与整库替换范围不匹配。虽然 `--replace-source` 帮助写了替换源库，计划输出未列出其他表将被删除；`--no-backup` 下原其他表没有该命令生成的备份可恢复。默认备份可以提供恢复来源，但不会让其他表继续保留在工作库中。
- **建议**：在临时库中保留未选中的表，只替换目标表，再发布完整库；尚未支持时，对多表库明确拒绝此模式。验收必须验证其他表的内容、契约和索引保留，并在 dry-run 中列出实际替换范围。

### R02 · P1：compact 失败留下重复可见 segment，校验未发现

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 1645 行 `_compact_partition()`、1744 行 `_write_compact_chunks()`、1818 行 `_mark_segments_obsolete()`。
- **原因**：新 chunk 写完即登记为可见；旧 segment 要等所有 chunk 完成才 obsolete。外层 `new_infos` 仅在整个 helper 返回后赋值，helper 中途抛错时，外层清理收到的仍是空列表。
- **实测**：4 行数据、每 segment 2 行，对 compact 的第二次文件写入注入 `OSError`；异常后查询得到 **6 行**，id 为 `[0,0,1,1,2,3]`。`validate(deep=True)` 返回 `errors=[]、warnings=[]`。见 `compact_failure`。
- **影响**：一次失败的维护任务改变查询结果；重试前已有重复数据可见。代码还存在新旧代同时可见的发布窗口，本次未另外运行并发读窗口探针。Parquet 继承相关流程，本次故障注入实测为 SQLite。
- **建议**：引入待发布 generation，全部文件成功后用一个元数据事务切换可见集合；异常和进程恢复只清理未发布代。校验应检查发布状态和代际一致性，不能把合法的跨 segment 重复主键一概当成损坏。验收覆盖第二个 chunk 失败、发布失败、重开库和重试。

### R03 · P1：segment 统计类型与 SQL 存储语义不一致，查询漏数据

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 2683 行 `_batch_column_stats()`、2764 行 `_increment_segment_info()`、3021 行 `_stats_excludes_case()`。
- **原因**：增量 MIN/MAX 从输入 Python 值计算，未统一到 SQLite 实际存储类型及比较语义。
- **实测**：声明 `v: text`，写入数值 `2`；SQLite 存储的是文本 `'2'`，manifest 的 min/max 却是数值 `2`。`Case('v', '>', 10)` 的公开查询返回空；相同物理文件上执行 `SELECT * FROM data WHERE v > ?`、参数 `10`，返回该行。见 `segment_affinity`。
- **影响**：裁剪改变结果而非仅改变执行代价。文本亲和性下 `'2' > '10'` 与 Python 数值比较不同，属于有效写入和有效查询的组合。
- **建议**：统计必须对应持久化值及可证明等价的比较语义；类型无法证明一致时保守禁用裁剪。修复不仅要转换统计值，还需处理谓词参数的亲和性。以原生 SQL 为对照，覆盖数值写入 text、混合输入、NULL 和各比较操作符。

### R04 · P1：显式 append 被表级默认写入模式覆盖

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 1245 行 `write()` 和 1154 行 `insert_all()`；Parquet 继承该入口。
- **原因**：`write(mode='append')` 转为 `replace=False, ignore=False`，下层又把“两者为 False”解释成未指定模式，再应用 `contract.conflict`。
- **实测**：表声明 `conflict='replace'`，先写 `{id:1,v:'before'}`，再显式 append `{id:1,v:'after'}`。SQLite 与 Parquet 都不报冲突，原行被替换成 `after`。见 `append_override`。
- **影响**：调用方显式选择的写入语义失效。表默认 ignore 的同型路径也会重选默认策略；该分支为源码判断，本次探针测的是 replace。
- **建议**：只在公开入口解析一次最终 mode；下层明确区分“未指定”与“显式 append”。增加默认 conflict 与显式 mode 的交叉契约用例，迁移/CLI 的显式 append 也应遵守相同规则。

### R05 · P1：覆盖重命名后，其他实例仍读旧目标

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 504 行 `rename()`；[database.py](../../iosql/database.py) 第 780 行 `rename_table()`、799 行 `_on_table_renamed()`；[coordination.py](../../iosql/coordination.py) 第 419 行 `_refresh()`。
- **实测**：两个 Database 打开同一路径。第一个实例的 `src` 为 id=1、`dst` 为 id=2；第二个实例先查询并缓存 `dst`。第一个执行覆盖重命名后，自己读 `dst` 为 id=1，第二个仍读出 id=2。见 `rename_cached_target`。
- **原因与影响**：所有者回调更新了本实例缓存，但命名空间变更未通过持久 generation 使其他实例丢弃旧连接和元数据。Linux 已打开文件可继续指向旧 inode，因此成功重命名后仍返回过时数据。
- **建议**：将重命名和覆盖发布纳入统一的代际失效协议；其他参与协作的实例在下一次操作前刷新。验收包含两个实例、两个进程、已读过目标和已缓存源的情形。本次没有据此声称跨进程或 Windows 已实测。

### R06 · P1：Parquet 有序流读在连接预算边界自锁

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 4157 行 `_iter_select_ordered_batches()` 的连接预留；[parquet/table.py](../../iosql/engines/parquet/table.py) 第 222 行 `_create_view()`；[connection_pool.py](../../iosql/utils/mysqlite/connection_pool.py) 第 99 行 `acquire()`。
- **原因**：继承的有序归并先预留全部物理流的连接配额，Parquet 真正开始扫描时又调用 `get_table_meta()` 申请 SQLite 元数据库连接。配额已被同一次查询占满，获取连接循环无 deadline，查询自身也无法推进并释放配额。
- **实测**：设置 `max_process_connections=2`，创建两个分区，调用 `iter_select(order_by='id', timeout_ms=500)`。6 秒内没有首行，子进程被探针结束；3 秒线程栈显示阻塞在 `acquire → get_table_meta → _create_view → _scan_rows`。见 `parquet_budget`。
- **影响**：合法预算配置导致无限等待，500ms 查询超时不能覆盖等待资源的时间。该实验使用缩小预算复现边界，没有测量默认预算下的大分区负载。
- **建议**：扫描前取得所需元数据，按后端分别计算真实资源需求，所有配额获取继承统一 deadline；无法满足时快速报错。验收必须包含恰好用满预算、取消查询、迭代器提前关闭和异常释放。

### R07 · P1：Web 分页遗漏跨 segment 重复主键

- **位置**：[web/app.py](../../iosql/web/app.py) 第 631–719 行的复合 keyset 分页。
- **原因**：将“排序列 + 全部主键列”视为唯一排序键，但 segment append 允许不同文件出现相同主键。局部约束不提供逻辑表全局唯一身份。
- **实测**：`Segment.rows(max_rows=1)`，连续 append 两行 `id=1`。第一页 `limit=1` 返回一行、游标 `[1]`；第二页返回空，另一行永远不可达。见 `web_pagination_and_stats`。
- **建议**：游标需要能区分物理行的稳定身份，或仅在有可证明的全局唯一键时启用此分页。尚不支持的布局应明确拒绝。不能通过查询时去重“修复”，因为重复行在该布局下是合法数据。验收覆盖重复排序值、重复主键、升降序和跨文件翻页。

### R08 · P2：strict schema 在新分区创建分支失效

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 4740 行起的 `except NoSuchTable` 分支，尤其新字段推断及 `_add_columns_for_all_partitions()`。
- **实测**：严格表仅声明 `id`，range 分区宽度 10；写 id=1 后，向新分区写 `{id:11, undeclared:'accepted'}` 成功，契约新增 `undeclared:text`，旧分区也被补列。见 `strict_partition`。
- **原因与影响**：新文件创建分支执行 schema 演进，没有先遵守 `alter=False`。同一输入是否被接受取决于是否落到新分区，破坏严格契约。
- **建议**：写入前按逻辑表契约统一校验整批字段，再创建文件；覆盖首次写入、新分区、新 segment 和已有分区，验证拒绝时不产生部分结构变更。

### R17 · P2：未知投影列静默成为字符串

- **位置**：[sqlite/query_util.py](../../iosql/engines/sqlite/query_util.py) 第 80 行 `build_select_fields_sql()`、194 行 `build_select_cases_query()`；[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 3493 行 `select()` 的投影处理。
- **实测**：仅有 `id` 列的表执行 `select(fields=['typo'])`，得到 `[{ '\"typo\"': 'typo' }]`，而非未知列异常。见 `unknown_projection`。
- **原因与影响**：字段仅被双引号转义，没有对契约/元数据做存在性校验；当前 SQLite 的双引号字符串兼容行为把拼错的标识符变成常量，掩盖调用错误。
- **建议**：在生成 SQL 前校验投影列，未知列统一抛出库级参数错误，覆盖 select/query/iter 路径及缺失物理列情形；不要依赖不同 SQLite 构建选项恰好拒绝这种 SQL。

## 性能与资源设计问题

### R09 · P2：无 ORDER BY 的 LIMIT 未下推

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 3530 行附近 `pushdown = bool(order_by) and limit is not None`，以及最终 Python 切片。
- **实测**：20,000 行表调用 `select(limit=1)`，底层执行 `select * from data ;`，读回 20,000 行才返回 1 行。见 `limit_fetch`。
- **影响**：探测一行也承担全部匹配数据的 I/O 和内存；Parquet 继承此调度路径。限制返回条数不能限制执行成本。
- **建议**：对无序查询维护全局剩余条数，逐文件下推 LIMIT 并及时停止；OR 去重等路径需保证全局语义。验收直接观察底层读行数，不能只断言返回 1 行。

### R10 · P2：OFFSET 分批读取重复扫描，迁移随之放大

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 1566 行 `iter_batches()`、4019 行 `_iter_part_query_batches()`；[migration.py](../../iosql/migration.py) 第 432 行 `_copy()` 使用源表批迭代器。
- **实测**：20,000 行、每批 100 行，API 与原生单游标 `fetchmany(100)` 都读到 20,000 行。前者约 **4,122,200** 条 SQLite VM 指令，后者约 **60,000** 条，工作量约 68.7 倍。见 `offset_vm_work`；这是指令数对照，不是耗时加速比。
- **原因与影响**：每批重新 `LIMIT ? OFFSET ?`，前面已读行被反复跳过。单物理文件的累计扫描工作近似 `O(N + N²/B)`；每批还是独立语句，不能保证同一读快照，并发删除可造成偏移变化。并发语义在旧报告中已有提示，本次新增量化工作量证据。
- **建议**：为无序读取同样使用专用只读连接、单游标和 `fetchmany`，保证关闭与取消时归还资源；明确跨文件快照边界。迁移、导出应复用这一有界读取原语。

### R11 · P2：segment ignore 批写变成逐行完整处理

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 4611 行 `_do_insert_segmented()`、2821 行附近主键查找、2846 行 `_write_segment_rows()`。
- **实测**：同一 segment 写 20 个不冲突的新主键，append 为 **1 次物理写入、0 次全量统计刷新**；ignore 为 **20 次物理写入、20 次全量统计刷新**。见 `segmented_ignore`。
- **影响**：每行扫描候选 segment 查重，新增行再走单行写入与统计；文件变大时，批量导入接近重复全扫描，并增加事务与元数据开销。Parquet 的整文件重写会进一步放大该流程的代价，相关组合未单独计时。
- **建议**：批量查找已有主键，将确认新增的行按目标文件合并写入；统计使用正确的增量或每批刷新，并保持 ignore 对批内重复键的顺序语义。验收比较调用次数和增长曲线，不能只测几十行的耗时。

### R12 · P2：Web 快速计数对 legacy 布局仍做全列扫描

- **位置**：[web/app.py](../../iosql/web/app.py) 第 407 行 `fast_row_count()`、第 650 行附近首页告警；[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 1401 行 `segments()`、2952 行 `_legacy_segment_info()`。
- **实测**：普通非 segment 表 1,000 行，请求 `/rows`、`limit=1`，仍调用一次 `_data_file_column_stats(None)`。见 `web_pagination_and_stats`。
- **原因与影响**：helper 注释将 `segments()` 描述为“零 I/O manifest 求和”，但 legacy 文件默认需要现场 COUNT 和各列 MIN/MAX。仅为了首页大表告警就触发统计，文件越多、列越宽越明显。这是 Web 调用诊断接口的遗留路径，不等同于已修复的普通查询前扫描再次失效。
- **建议**：分离轻量布局信息、估计/缓存计数和精确统计；首页告警不应强制精确全库扫描。精确 `/stats` 也应避免重复获取同一统计。验收覆盖 legacy、segment 和混合布局，并对普通首页禁止调用全列统计 helper。

### R13 · P2：PostgreSQL 控制连接开销落在每次独立 API 调用上

- **位置**：[coordination.py](../../iosql/coordination.py) 第 450 行 `guarded()` 与 lease 路径；[coordination_postgresql.py](../../iosql/coordination_postgresql.py) 第 51 行 `connect_control()`、68 行 `acquire()`、86 行 `release()`、100 行 `state()`。
- **证据**：mock 控制连接后，连续独立调用 3 次 `PgTable.spec()`，观察到 **3 次新建连接、3 次关闭、9 条 SQL**：共享锁、状态表存在性检查、解锁各一次。控制表存在时还会读取状态行。见 `pg_control_calls`。
- **影响**：契约访问等轻操作也附带连接认证和多次网络往返；高频小查询会受固定成本限制。嵌套在可复用 lease 内的调用不应与此独立调用计数混淆。本次没有 PostgreSQL 服务，因此没有实测毫秒数或吞吐损失。
- **建议**：保持迁移互斥语义，复用具有明确所有权的控制会话或 lease，并缓存稳定的资源身份；评估 generation 检查频率和失效机制。live PG 验收需包含连接次数、往返次数、迁移互斥、断连、fork 和异常释放，不能仅为提速删除协调锁。

### R14 · P2：Parquet 批次追加重复读取、重写完整文件

- **位置**：[parquet/table.py](../../iosql/engines/parquet/table.py) 第 254 行 `_mutable_table()`、275 行 `_publish()`、290 行 `_do_insert_part()`；[migration.py](../../iosql/migration.py) 的逐批写入。
- **实测**：同一物理文件连续写 5 批，每批 100 行；各批重载旧行数为 `[0,100,200,300,400]`。新增 500 行，却累计输出 1,500 行到完整替换文件。见 `parquet_batch_rewrite`。
- **影响**：在固定批大小下，单文件累计写出行数为 `B × (1 + 2 + … + K)`，随批次数平方增长。大分区迁移和频繁小批追加尤其明显；已有设计说明存在整文件发布，本报告将其列为保留的性能限制。
- **建议**：优先适配不可变 segment/row group 的追加结构，或迁移时为一个目标分区建立一次 staging 写入会话，最后统一发布。增大批次只能缓解，不能消除增长阶数；同步明确峰值内存和临时磁盘预算。

### R15 · P2：若干高层便利操作仍然全量物化

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 2135 行 `_latest_rows()`、1645 行 `_compact_partition()`、4347 行 `upsert_all()`；latest 查询对象的 `iter_rows()` 也会调用 `to_list()`。
- **源码确认**：latest 先取出全部候选行再排序/分组；compact 读完整逻辑分区后排序、分块；低层 `upsert_all()` 先全表查询、按主键在 Python 合并，再将全部结果写回。
- **影响**：返回少量 latest 或更新一个键，仍可能处理整表；compact 的内存随分区规模增长。“iter” 外观并不保证流式。这里没有新跑大数据耗时，且低层 upsert 的限制已经写入代码注释，不能计为新回归。
- **建议**：latest 下推每组候选或使用数据库窗口查询；compact 用外部归并及明确内存预算；低层 upsert 按受影响主键读取和更新。保留便利 API 时应公开复杂度和适用规模，避免迁移/后台任务默认选中全量路径。

### R16 · P2：逻辑索引定义未传播到未来文件

- **位置**：[sqlite/table.py](../../iosql/engines/sqlite/table.py) 第 4322 行 `create_index()`，只遍历当前 `_physical_part_refs()`。
- **实测**：range 分区先写 id=1，创建 `v` 索引，再写 id=11 产生新分区。PRAGMA 显示分区 0 有 `data_v_index`，分区 10 没有。见 `index_future_partition`。
- **影响**：同一逻辑表的历史数据与新数据执行计划不同；调用方无法依靠一次 create_index 持续维护索引。compact 产生新文件时也需要核对索引继承，本次运行证据仅覆盖新增分区。
- **建议**：在逻辑契约/元数据中持久化索引定义，新分区、新 segment、compact 和迁移都统一应用；drop_index 同步更新逻辑定义。验收检查新旧文件的实际索引和查询计划。

## 已有记录仍需保留的问题

以下内容来自当前工作区已有记录和对应源码核对；历史数字不代表本次重新测量，也没有纳入上述问题数。

| 已有记录 | 当前判断 | 建议 |
| --- | --- | --- |
| [固定结构写入仍全量推断类型](../known-issues/KI-20261021-1806-002-write-type-inference.md) | `mysqlite/table.py:528` 每批仍调用 `_union_fields_of_dictlist()`，固定字段写入也付出类型遍历成本 | 将字段分组、必要校验和类型推断分开；固定契约快路径仍需保持异构行顺序和类型拒绝语义 |
| [逐行缓冲重复解析协作资源](../known-issues/KI-20261021-2357-001-buffered-append-lease.md) | buffered writer 已持有上下文时，逐行追加仍有资源身份解析成本 | 在生命周期内缓存稳定资源身份并复用 lease；不要绕过迁移保护 |
| [连接池预算遍历偶发异常](../known-issues/KI-20261021-1930-001-pool-budget-iteration.md) | 先前组合回归记录了 `dictionary changed size during iteration`；本次扩展回归未再次出现 | 保留未解决状态，针对弱引用注册表与并发生命周期做复现；不能因单次通过而关闭 |
| [PLAN-092 真实导入分析](../dev/PLAN-092-model-unknown-live-import-profile-OUTCOME.md) | 既有 HDD 回放记录 commit+close 约占总时间 71%，不能与包含它们的 write 阶段重复相加 | 优先减少跨分区批次和连接淘汰造成的重复提交/关闭；CPU 推断优化不能直接等同于 HDD 总吞吐收益 |

## 设计层面的主要改进方向

### 1. 把发布和失效收敛到同一套生命周期

迁移已经有锁、staging、恢复和 generation 概念，但 compact、重命名和单表 CLI 替换未完整共享相同不变式，分别表现为 R02、R05、R01。需要统一保证：构建中的文件不可见；发布切换可原子完成；失败后保留旧可用集合；所有实例在代际变化后失效旧句柄；操作范围与用户选中的对象一致。

可以先修三个具体入口，再抽取共用发布协议。只增加异常清理，不能解决读者看到半发布状态的问题；只更新当前实例缓存，也不能解决其他实例继续持有旧连接的问题。

### 2. 将文件布局公共逻辑与 SQLite 执行器分开

`sqlite/table.py` 约 5,300 行，同时负责契约、分区/segment、元数据、查询调度、SQL 执行、写入、索引和维护。Parquet 直接继承它，复用了文件布局能力，也继承了与 SQLite 连接预算、模式映射和限量查询有关的假设；R04、R06、R09 是具体后果。

建议逐步抽出少量内部能力：目录/manifest 管理、后端扫描与批写、统计与比较语义、发布协调。由后端明确是否支持原位更新、索引、持久游标及资源计量。不要先重写整个库；以修复跨后端缺陷为边界逐步拆分。

### 3. 让“轻量元数据”和“昂贵诊断”成为不同接口

当前 `segments()` 同时承担布局枚举和现场统计，使 Web 误把它当作零 I/O 操作；而统计既是性能优化输入，又直接决定是否跳过数据。应区分布局引用、持久统计、精确重算，并标注统计是否完整、是否适用于当前类型语义。优化失败时应退回扫描，不能牺牲结果完整性。

### 4. 统一公开契约，避免各层重新解释

需要一份跨 SQLite/Parquet/PostgreSQL 的执行契约矩阵，至少覆盖 mode 默认值与显式值、主键作用域、strict/evolve、NULL、未知列、迭代返回形状、超时范围和事务边界。R04 的二次默认值解析、R07 的唯一性假设、R08 的冷路径特例和 R17 的 SQL 方言行为，说明仅统一方法签名不足以保证同一 API 的语义一致。

### 5. 对核心路径建立可测的成本约束

接口名不应代替资源保证。应能明确回答：取 1 行最多读多少行、每批写入多少次、一次导出持有几个连接、一个查询的 deadline 是否覆盖连接等待、临时文件和峰值内存如何限制。优先用 SQL/调用计数建立稳定回归，再对代表性数据量测延迟和吞吐，避免把某台机器的耗时阈值当作唯一保障。

## 建议修复顺序与验收

1. **数据安全**：R01、R02、R05。覆盖多表库保留、compact 第二个 chunk 失败、发布中断、两实例/进程旧句柄失效；发布后重新打开验证，不只检查内存对象。
2. **结果与写入契约**：R03、R04、R07，并处理 R08、R17。采用原生 SQL 对照、mode 交叉矩阵和完整分页遍历，证明优化前后行集合与重复次数一致。
3. **资源阻塞**：R06。针对预算恰好耗尽、取消、异常及提前关闭验证 deadline 和资源释放；随后在 live PG 中验证 R13 的协调开销与安全复用方案。
4. **有证据的性能改进**：R09、R10、R11、R12、R14、R16，以及已有的类型推断/缓冲路径问题。比较操作计数随 N、批大小、分区数的增长，再测真实分区分布和磁盘介质。
5. **长期边界与质量门禁**：处理 R15 的全量算法，厘清 6 项回归失败，保留 wheel 隔离冒烟，并补 live PG、Windows 和容量测试。现有 945 项通过提供基础，但本次多个 2–4 行即可触发的问题仍未被覆盖。

## 证据与复现

- [probes.py](20261022/probes.py)：15 个独立探针，每个仅写入自动清理的临时目录；不接受业务库路径。`pg_control_calls` 不连接真实 PostgreSQL。`parquet_budget` 主动在 6 秒后结束阻塞子进程，超时是该探针的预期证据。
- [probes.jsonl](20261022/probes.jsonl)：本次最终完整运行结果，包含阻塞栈和全部计数。它是审查观测记录，不能当作修复后的通过基线；修复后对应结果应改变。
- [pytest-output.txt](20261022/pytest-output.txt)、[ruff-output.txt](20261022/ruff-output.txt)、[validation.json](20261022/validation.json)：回归、静态检查及环境/产物验证。

本机复现命令（仓库根目录，使用已有依赖环境）：

```bash
PYTHONPATH=. uv run --no-sync /home/lsl/miniconda3/bin/python docs/review/20261022/probes.py
uv run --no-sync /home/lsl/miniconda3/bin/python -m pytest -q tests -k 'not TestLargeData'
uv run --no-sync /home/lsl/miniconda3/bin/ruff check iosql cli examples benchmarks --output-format concise
```

在其他机器上需替换解释器路径并安装 SQLite/Parquet/Web 测试依赖。探针的 `error` 字段表示探针自身未完成，应单独检查；预期的 compact 异常和 Parquet 阻塞由对应探针内部记录在 `result` 中。
