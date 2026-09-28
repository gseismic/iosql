# PLAN-072 全库 Review 问题修复与多用户并发评估结果

- 日期：2026-09-29
- 计划：`docs/dev/PLAN-072-full-review-fixes-and-concurrency.md`
- 问题清单：`docs/review/code-review-20260929-full.md`
- 并发设计：`docs/design/multi-user-concurrency-20260929-overview.md`

## 结论

报告中的 P0×3、P1×8、P2×4 均已修复或在无全局索引的场景显式拒绝不安全操作。发现并修复额外的同机多进程 segment 写/compact 竞争：原代码可在两个成功写入后留下错误 manifest 行数；现在同表复合写入由跨进程文件锁串行化。跨分区读取和跨文件崩溃原子性仍是明确的架构边界，设计文档给出了演进路径。

## 逐项结果

| 报告项 | 修复 | 回归证据 |
| --- | --- | --- |
| P0-1 wheel 漏 Web 资源 | `pyproject.toml` 显式纳入 `iosql/web/static/**`。 | `tests/test_distribution.py` 构建 wheel、隔离安装并请求首页/CSS/JS；手工检查 wheel 和 sdist 均含 12 个静态文件，并将本轮 sdist 隔离安装后验证首页、CSS 与主要 JS 均返回 200。 |
| P0-2 表覆盖重命名失败丢目标 | `Table.rename()` 统一暂存目标、回滚源/目标；覆盖前验证真实表；表级 manifest 多表更新放进一个 SQLite 事务。`Database.rename_table()` 的 SQLite 路径委托该实现。 | `tests/test_plan072_fixes.py`：移动失败、manifest 失败、普通目录拒绝、成功覆盖；PLAN-062 旧故障注入用例通过。 |
| P0-3 restore 身份检查不足 | 新增只读表/库身份校验，校验 manifest、SQLite `quick_check`、引用文件、segment 行数；备份、覆盖目标及复制副本均校验；空目录无身份标记，拒绝当作备份。 | 普通备份、伪造 SQLite 目标、缺分区文件、行数不符备份均拒绝且原目标不变；拒绝非法备份前后的目标文件字节快照一致；现有合法覆盖恢复用例通过。 |
| P1-1 分区 upsert 重复主键 | 分区列不在主键时，SQLite `write(upsert/replace)` 与 `insert_all(replace=True)` 立即报 `BadArgument`，包括首次低层写入且尚无持久化契约的入口。`append/ignore` 的唯一性边界在 README/skill 说明。 | 跨分区同主键回归验证拒绝写入且原行数仍为 1；低层首次 `replace=True` 拒绝并保持空表。 |
| P1-2 直接表重命名缓存失效 | SQLite/PG 表对象保留 Database 弱引用，重命名成功同步 `_tables`；PG `Database.rename_table(overwrite=True)` 正确传给引擎。 | 直接重命名和覆盖后旧名不返回新表对象，目标名返回原对象；PG mock 验证覆盖 SQL 与缓存。 |
| P1-3 数字文本图表漏点 | SQLite/PG 聚合用数值型 SQL 边界，SQLite 校验 64 位溢出；PG 校验 bigint 范围；支持负整数文本。 | SQLite `2/10/3` 三行计数、跨分区边界合并、Web line 请求、负值和溢出；PG mock 验证 SQL；临时本机 PG 17 实测 `2/10/3` 全部入桶、负值边界正确、越界整数报 `BadArgument`。 |
| P1-4 candle 开收盘乱序 | 首遍按数值求横轴范围，桶内用最小/最大横轴值选 open/close；同时间 open 取首次扫描、close 取末次非空。 | 乱序及同时间回归返回最早 open、最晚 close。 |
| P1-5 无主键 keyset 漏行 | Web `/rows` 对无主键表直接 422，提示声明唯一主键或导出。 | 同排序值无主键表回归得到 422。 |
| P1-6 PG `iter_rows` 返回批 | 增加 PG `iter_batches()`；`iter_rows()` 展平为逐行，PG 迁移改用 `iter_batches()`。实库复核又发现 psycopg2 命名游标在首次 `fetchmany()` 前 `description=None`，已将列名读取推迟到首批之后。 | 延迟描述 mock 回归、真实 PG 17 逐行与分批迭代、PG→SQLite 迁移及全量迁移套件通过。 |
| P1-7 普通目录误枚举/误写 | `table_names()` 只列真实 manifest；`Database.__getitem__` 拒绝在有普通内容的目录创建元数据，允许未写入空表的元数据库 scaffold 重新打开。 | 普通 `notes/readme.txt` 不在表名单，`db.table('notes')` 报错且未生成 meta；空表 scaffold 可重开。 |
| P1-8 PG 固定 public schema | 表枚举使用 `coalesce(current_schema(),'public')`，与表引擎解析一致。 | SQL mock 断言；临时 PG 17 设置非 public `search_path`，仅列出该 schema 的 `txt_bounds`，未混入 public 标记表。 |
| P2-1 `force_partition` 默认为 False 覆盖 spec | 快捷参数改用 `None` 区分未传；`spec=` 保留契约原值，显式冲突报错。 | True 契约未传保持 True，显式 False 拒绝。 |
| P2-2 Web 非法 `order_by` 500 | 校验仅有一个非空列及可选方向，不接受额外逗号或 token。 | `","`、`"ts desc extra"` 返回 422。 |
| P2-3 README 默认行类型错误 | `select()` 文档改为默认 dict、`to_dict=False` 为元组。 | 与 SQLite/PG 签名对照。 |
| P2-4 CLI 非回环 Host 不可访问 | `create_app` 接受具体 Host 白名单；CLI 对具体绑定地址自动加入，对 `0.0.0.0` 要求 `--allowed-host`。 | CLI mock 启动后指定 Host 返回 200、未列入 Host 返回 400；通配绑定未提供白名单拒绝。 |

## 并发实测与限制

- 修复前两进程同写一个剩余容量为 1 的 segment：物理 3 行、manifest 2 行，深度校验失败；修复后的确定性同场景和 compact/write 交错回归均通过。
- 两个进程写不同分区、一个进程反复读取的探针：全部正常退出，最终 101 行，`validate(deep=True).ok=True`。读者运行中看到的行数变化证明不能把连续查询视为统一快照。
- 锁限于遵守 iosql API 的同机进程。`rename`、`restore`、在线备份与重分区没有在线切换协议，应在维护窗口停写并关闭连接。跨文件崩溃一致性、多机共享目录、用户级认证授权不在本轮实现范围，详见并发设计文档。

## 验证

- 定向安全/正确性/发布测试覆盖故障注入、Web、PG mock 与隔离 wheel 冒烟；PG 迁移集成测试补充逐行/分批返回形状断言后，使用实库单独复跑通过。
- `POSTGRES_DSN=postgresql://lsl@127.0.0.1:55487/postgres python -m pytest tests/ -q --disable-warnings`：**748 passed，13 skipped，69 warnings**，99.09 秒。PostgreSQL 是本轮创建的临时本机实例，未使用或修改常驻服务。
- 从当前工作树构建 sdist，隔离安装于 `/tmp` 后确认导入来源，并请求 `/`、`/style.css`、`/app.js`、`/js/components.js`、`/vendor/uplot.min.js`，均返回 200。
- `python -m compileall -q iosql cli examples benchmarks`、非 vendor JS `node --check`、`git diff --check`：通过。
- PostgreSQL 实现和回归文件 `ruff check` 通过；`iosql/engines/sqlite/table.py` 仍有 4 个既有 lint 错误，本计划没有改写全库 lint 基线。
- PG 17 实库额外验证：非 public schema 枚举、数字文本边界、负值与溢出、命名游标逐行/分批读取、PG→SQLite 迁移、直接/覆盖重命名；临时实例在验证后关闭并清理。

## 发布与使用注意

wheel 与 sdist 打包安装的 Web 资源均已在隔离目录验证。`--allowed-host` 只解决 HTTP Host 匹配；Web 管理台没有内置多用户认证。多用户服务需要独立认证授权入口，数据目录应只由可信同机进程访问。
