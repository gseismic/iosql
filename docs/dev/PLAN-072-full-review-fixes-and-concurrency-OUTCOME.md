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
| P0-1 wheel 漏 Web 资源 | `pyproject.toml` 显式纳入 `iosql/web/static/**`。 | `tests/test_distribution.py` 构建 wheel、隔离安装并请求首页/CSS/JS；手工检查 wheel 12 个静态文件、sdist 12 个静态文件。 |
| P0-2 表覆盖重命名失败丢目标 | `Table.rename()` 统一暂存目标、回滚源/目标；覆盖前验证真实表；表级 manifest 多表更新放进一个 SQLite 事务。`Database.rename_table()` 的 SQLite 路径委托该实现。 | `tests/test_plan072_fixes.py`：移动失败、manifest 失败、普通目录拒绝、成功覆盖；PLAN-062 旧故障注入用例通过。 |
| P0-3 restore 身份检查不足 | 新增只读表/库身份校验，校验 manifest、SQLite `quick_check`、引用文件、segment 行数；备份、覆盖目标及复制副本均校验；空目录无身份标记，拒绝当作备份。 | 普通备份、伪造 SQLite 目标、缺分区文件、行数不符备份均拒绝且原目标不变；现有合法覆盖恢复用例通过。 |
| P1-1 分区 upsert 重复主键 | 分区列不在主键时，SQLite `write(upsert/replace)` 与 `insert_all(replace=True)` 立即报 `BadArgument`。`append/ignore` 的唯一性边界在 README/skill 说明。 | 跨分区同主键回归验证拒绝写入且原行数仍为 1。 |
| P1-2 直接表重命名缓存失效 | SQLite/PG 表对象保留 Database 弱引用，重命名成功同步 `_tables`；PG `Database.rename_table(overwrite=True)` 正确传给引擎。 | 直接重命名和覆盖后旧名不返回新表对象，目标名返回原对象；PG mock 验证覆盖 SQL 与缓存。 |
| P1-3 数字文本图表漏点 | SQLite/PG 聚合用数值型 SQL 边界，SQLite 校验 64 位溢出；PG 校验 bigint 范围；支持负整数文本。 | SQLite `2/10/3` 三行计数、Web line 请求、负值和溢出；PG mock 验证数值边界 SQL 与分桶参数。无 live PG DSN。 |
| P1-4 candle 开收盘乱序 | 首遍按数值求横轴范围，桶内用最小/最大横轴值选 open/close；同时间 open 取首次扫描、close 取末次非空。 | 乱序及同时间回归返回最早 open、最晚 close。 |
| P1-5 无主键 keyset 漏行 | Web `/rows` 对无主键表直接 422，提示声明唯一主键或导出。 | 同排序值无主键表回归得到 422。 |
| P1-6 PG `iter_rows` 返回批 | 增加 PG `iter_batches()`；`iter_rows()` 展平为逐行，PG 迁移改用 `iter_batches()`。 | mock 验证两种返回形状；迁移套件通过。 |
| P1-7 普通目录误枚举/误写 | `table_names()` 只列真实 manifest；`Database.__getitem__` 拒绝在有普通内容的目录创建元数据，允许未写入空表的元数据库 scaffold 重新打开。 | 普通 `notes/readme.txt` 不在表名单，`db.table('notes')` 报错且未生成 meta；空表 scaffold 可重开。 |
| P1-8 PG 固定 public schema | 表枚举使用 `coalesce(current_schema(),'public')`，与表引擎解析一致。 | SQL mock 断言；无 live PG DSN。 |
| P2-1 `force_partition` 默认为 False 覆盖 spec | 快捷参数改用 `None` 区分未传；`spec=` 保留契约原值，显式冲突报错。 | True 契约未传保持 True，显式 False 拒绝。 |
| P2-2 Web 非法 `order_by` 500 | 校验仅有一个非空列及可选方向，不接受额外逗号或 token。 | `","`、`"ts desc extra"` 返回 422。 |
| P2-3 README 默认行类型错误 | `select()` 文档改为默认 dict、`to_dict=False` 为元组。 | 与 SQLite/PG 签名对照。 |
| P2-4 CLI 非回环 Host 不可访问 | `create_app` 接受具体 Host 白名单；CLI 对具体绑定地址自动加入，对 `0.0.0.0` 要求 `--allowed-host`。 | CLI mock 启动后指定 Host 返回 200、未列入 Host 返回 400；通配绑定未提供白名单拒绝。 |

## 并发实测与限制

- 修复前两进程同写一个剩余容量为 1 的 segment：物理 3 行、manifest 2 行，深度校验失败；修复后的确定性同场景和 compact/write 交错回归均通过。
- 两个进程写不同分区、一个进程反复读取的探针：全部正常退出，最终 101 行，`validate(deep=True).ok=True`。读者运行中看到的行数变化证明不能把连续查询视为统一快照。
- 锁限于遵守 iosql API 的同机进程。`rename`、`restore`、在线备份与重分区没有在线切换协议，应在维护窗口停写并关闭连接。跨文件崩溃一致性、多机共享目录、用户级认证授权不在本轮实现范围，详见并发设计文档。

## 验证

- 定向安全/正确性/发布测试：24 passed；覆盖故障注入、Web、PG mock 与隔离 wheel 冒烟。
- `python -m pytest tests/ -q --disable-warnings`：**742 passed，16 skipped，68 warnings**，103.07 秒。
- `python -m compileall -q iosql cli examples benchmarks`、非 vendor JS `node --check`、`git diff --check`：通过。
- 新增模块和回归文件 `ruff check`：通过；既有模块仍有此前 review 记录的 lint 错误，本计划没有改写全库 lint 基线。
- PostgreSQL live 测试：`POSTGRES_DSN` 未设置；PG 聚合、枚举与迭代接口采用 mock 验证。需要在有 PG 实例的环境补 live 回归。

## 发布与使用注意

打包安装的 Web 资源已在隔离目录验证。`--allowed-host` 只解决 HTTP Host 匹配；Web 管理台没有内置多用户认证。多用户服务需要独立认证授权入口，数据目录应只由可信同机进程访问。
