# iosql 全代码库 Review（2026-09-29）

- 审查基线：`2a2e6bc`（PLAN-071 后），Python 3.12；工作区原有未跟踪文件 `.zcodeignore` 未参与审查。
- 范围：公共 API 与契约、分区和查询层、SQLite 与 PostgreSQL 引擎、mysqlite、CLI、Web 后端与前端、构建配置、README/skill、示例、测试与 benchmark 入口。
- 依据：当前源码和测试、`docs/review/code-review-20260918-full.md`、`docs/dev/PLAN-062-full-review-fixes-OUTCOME.md`；本文只列当前仍存在的问题。标为“实测”的条目均在临时目录用当前代码复现，未修改业务数据。

## 结论

**当前 wheel 不宜发布。** 构建出的 wheel 缺少全部 Web 静态资源，隔离安装后首页报错。另有公开的 `Table.rename(overwrite=True)` 失败时丢失目标表、`restore` 对备份/目标的身份判断不足，以及跨分区 `upsert` 留下重复逻辑主键的风险。全量回归通过，说明这些边界没有被现有测试覆盖。

本轮发现 **P0 3 项、P1 8 项、P2 4 项**。P0/P1 是需要排期修复的行为缺陷；P2 主要是输入校验与接口/文档一致性。上一轮 PLAN-062 的 10 项 P0 修复有回归测试，本轮没有重复宣称逐项重演；上一轮报告的 **P0-11（wheel 漏静态资源）未纳入 PLAN-062 批次 A，当前仍存在**。

### 验证结果

| 检查 | 当前结果 | 解释 |
| --- | --- | --- |
| `python -m pytest tests/ -q` | **723 passed, 16 skipped, 68 warnings**，91.57s | 源码树回归通过；跳过项包含依赖 live PostgreSQL 的测试。 |
| `python -m compileall -q iosql cli examples benchmarks` | 通过 | 当前解释器下语法编译通过。 |
| 非 vendor 前端 JS 的 `node --check` | 通过 | 仅是语法检查，不代表浏览器交互正确。 |
| `ruff check iosql cli tests benchmarks examples --output-format concise` | **63 errors** | 多数是未使用导入；核心还有 `database.py:995` 无插值 f-string、`web/app.py:836` 易混淆变量名。 |
| `python -m pip wheel . --no-deps` | 构建成功，50 个 wheel 成员，**0 个 `iosql/web/static/**`** | 构建成功不能证明可发布。 |
| wheel 隔离安装后 `GET /` | `StaticFiles directory .../iosql/web/static does not exist` | 已确认源码树与安装产物行为不同。 |

## P0：发布与数据安全

### P0-1 wheel 未包含 Web 静态资源（实测）

- 位置：`pyproject.toml:38-43` 只有 `include-package-data = true`，没有 `package-data` 或 `MANIFEST.in`；`iosql/web/app.py:1407-1413` 运行时挂载 `web/static`。
- 证据：当前仓库跟踪了 12 个 `iosql/web/static` 文件，但新构建的 `iosql-0.3.0-py3-none-any.whl` 内为 0 个。将 wheel 装入 `/tmp/iosql-review-install` 并从 `/tmp` 导入，`iosql.__file__` 指向隔离安装目录，`static` 不存在；请求首页触发上述 `RuntimeError`。
- 影响：`pip install iosql[web]` 的用户无法打开管理台；后端接口可能工作，首页及 JS/CSS 均不可用。PLAN-062 的测试均从源码树导入，未覆盖产物。
- 建议：在构建配置中显式包含 `iosql.web` 的 `static/*`、`static/js/*`、`static/vendor/*`，构建 wheel 后在干净目录安装并验证 `/`、`/style.css` 和主要 JS 路径。

### P0-2 直接调用 `Table.rename(overwrite=True)` 时移动失败会删除目标表（实测）

- 位置：`iosql/engines/sqlite/table.py:438-472`，尤其 `:463-466` 先 `shutil.rmtree(target_path)` 再 `os.replace(source_path, target_path)`，没有失败回滚；`iosql/database.py:778-822` 的上层入口虽暂存目标，却没有验证目标确为 iosql 表。
- 证据：临时库创建 `src` 与 `dst` 两张真实表，给 `os.replace` 注入 `OSError("move failed")` 后执行 `src.rename("dst", overwrite=True)`；调用报错，`src` 目录仍在，**`dst` 目录已消失**。`Database.rename_table("src","notes",overwrite=True)` 对库内仅含 `keep.txt` 的普通目录也会成功覆盖，`keep.txt` 消失。README 展示了直接 `Table.rename()` 形式。
- 影响：磁盘/权限/跨设备等移动异常时，覆盖目标数据不可恢复；目标不是表时，两种入口都可能清除普通目录。`overwrite=True` 表达覆盖目标表的意图，不等于授权删除普通目录或“失败后也删除目标”。
- 建议：让公开的 `Table.rename()` 与 `Database.rename_table()` 共用“先暂存目标、移动源、失败回滚”的单一实现；覆盖前验证目标确为 iosql 表；增加真实目标表 + 故障注入回归。

### P0-3 `restore --replace` 的源/目标身份检查不足，可覆盖非预期目录（实测）

- 位置：`iosql/cli.py:1993-2007` 只要求 `backup_path` 是目录；`:2029-2038` 对目标调用 `_is_iosql_like_database()`，却没有校验备份；该 helper 在 `:200-216` 只查子目录里某个 `meta.sqlite3` 的 SQLite 文件头，不查 iosql manifest；`:2044-2074` 直接复制并换入。
- 证据 A：临时目录里创建含 `t` 表和一行数据的真实 `target.iosql`，另建只含 `note.txt` 的普通目录 `not_a_backup`。执行 `restore --backup not_a_backup --target target.iosql --replace --yes`，退出码 **0**，目标库只剩 `note.txt`，原表目录消失。
- 证据 B：在普通目标目录 `ordinary` 放 `keep.txt` 和 `fake_table/meta.sqlite3`（仅有 `CREATE TABLE unrelated(x)` 的普通 SQLite 文件），使用真实备份执行同样的覆盖恢复，退出码 **0**，`keep.txt` 消失。上轮 P0-4 的“伪造标记”风险仍可通过有效 SQLite 文件头绕过。
- 影响：误选、空/坏备份会在“恢复成功”状态下清除真实数据库；普通目录也可被错误识别为 iosql 库而被覆盖。目标换入回滚无法防止错误内容被成功换入。
- 建议：复制前验证备份和目标的真实 iosql manifest、表元数据库与必要分区文件；在临时副本上完成深度校验后才允许替换目标，并明确合法空库的识别规则。加入普通目录、伪造 SQLite 文件、缺失分区文件和损坏文件用例。

## P1：结果正确性和接口一致性

### P1-1 分区表 `upsert` 可留下重复逻辑主键（实测）

- 位置：`iosql/engines/sqlite/table.py:1111-1157` 根据新行分区直接写目标物理文件；每个文件只保证本地主键约束。`_ensure_segment_conflict_supported()` 只限制 segment 的 upsert/replace，没有检查主键与逻辑分区的关系。
- 复现：`pk=["id"]`、`partition=Partition.range("bucket", every=10)`，先写 `{id:1,bucket:1,v:100}`，再以 `mode="upsert"` 写 `{id:1,bucket:11,v:200}`。`select()` 返回两行，`count_rows()` 为 2，分区为 `[0,10]`。
- 影响：用户把主键视为逻辑表唯一键时，`upsert`/`replace` 无法覆盖旧行，查询、迁移与计数会保留双份数据。旧 review 已提到同一根因，PLAN-062 未关闭。
- 建议：若未实现全局主键索引，至少在声明/写入时要求分区键属于主键，或对 `upsert`/`replace` 的跨分区可能性显式拒绝；文档明确 `append` 的唯一性边界。

### P1-2 直接 `Table.rename()` 成功后 `Database` 缓存仍指向旧名字（实测）

- 位置：`iosql/engines/sqlite/table.py:468-470` 更新表对象名字；`iosql/database.py:177-199` 的 `_tables` 缓存仍以旧键保存。只有 `Database.rename_table()` 在 `:823-825` 更新缓存。
- 证据：`t = db.table("old")` 后 `t.rename("new")`，目录列表只有 `new`，但 `db.table("old") is t` 为真且返回对象 `table_name == "new"`；`db.table("new") is t` 为假。
- 影响：调用方继续用旧名字获取表时，会无提示地读写新表，违反表名路由语义。README 明确把 `Table.rename()` 作为可用入口。
- 建议：将直接重命名委托给有缓存更新能力的 `Database`，或给表对象维护 owner 回调；至少不允许旧缓存键继续返回已改名对象。

### P1-3 纯数字文本横轴在变长数值时漏掉图表数据（SQLite 实测，PG 静态同因）

- 位置：`iosql/engines/sqlite/query_util.py:262-265` 对文本列做 `MIN/MAX`；`iosql/engines/sqlite/table.py:3463-3467,3505-3513,3535-3538` 再把词典序边界转整数，越界桶直接丢弃。PG 的同型路径在 `iosql/engines/postgresql/table.py:1118-1140,1165-1198`。
- 证据：`x` 为 text，写入 `"2"/"10"/"3"`，各有非空数值 `y`。`aggregate_by_bucket("x",["y"],4)` 返回 `scanned=3`、`x_mode='text'`，却得到 `x_min='10'`、`x_max='3'`、`sum(counts['y'])=1`；另外两行被丢弃。声明 `order_key='x'` 后请求 Web line chart，HTTP 200 但只返回 `x=[10]`、`y=[10.0]`。
- 影响：Web line 图无错误地显示不完整曲线。固定宽度 `YYYYMMDD` 不触发此例，但接口承诺支持“纯数字文本”，范围不限于定宽日期。
- 建议：对文本模式在 SQL 内按数值 `CAST` 求边界，并检验溢出/前导空格语义；补跨分区、变长与负号/大整数测试。PG 路径需 live PG 验证修复。

### P1-4 Candle 接口把扫描顺序当作开收盘顺序（实测）

- 位置：`iosql/web/app.py:925-927,995-1010` 两次 `iter_select_batches()` 都没有 `order_by`；第一条作为 open，最后一条作为 close。
- 证据：同一桶依次写入 `ts=3/open=30/close=30`、`ts=1/open=10/close=10`、`ts=2/open=20/close=20`。`GET /api/table/bars/chart?col=close&kind=candle&buckets=1` 返回 `open=[30]`、`close=[20]`；按时间应为 `open=[10]`、`close=[30]`。
- 影响：历史回填、乱序到达或跨 segment 数据会生成错误 K 线；high/low 正常并不能发现开收盘错位。
- 建议：按横轴时间排序或在桶内比较最小/最大时间确定 open/close；同时明确相同时间的稳定次序。前端当前用四次 line 请求绘图，此缺陷仍影响公开 candle API。

### P1-5 无主键表的 Web keyset 分页静默漏同值行（实测）

- 位置：`iosql/web/app.py:626-632,668-705` 只有主键存在时才追加稳定次序；无主键时游标只含排序列，下一页用严格 `>`。
- 证据：用底层 `insert_all()` 建立合法的无主键表，行按 `ts` 为 `(1,a),(1,b),(2,c)`。`POST /rows` 请求 `order_by="ts",limit=1`：第一页为 `a`、`next_cursor=[1]`，第二页直接为 `c`，`b` 永远不可达。
- 影响：Web 数据浏览分页无法完整遍历旧表或低层 API 建立的无主键表；响应没有截断/不稳定提示。`/export` 采用独立的流式路径，不受此问题影响。
- 建议：无唯一排序键时拒绝 keyset 并给可操作提示，或设计跨分区稳定的物理行标识作为次级键；补同值跨页测试。

### P1-6 PostgreSQL `iter_rows()` 实际产出批列表（静态 + 方法级实测）

- 位置：`iosql/engines/postgresql/table.py:1217-1223` 直接 `yield from self.iter_select_batches(...)`；SQLite 的同名方法在 `iosql/engines/sqlite/table.py:1443-1447` 逐行展开，README `:493` 将 `iter_rows()` 定义为逐行语法糖。
- 证据：用受控 `iter_select_batches` 产出 `[[{'id':1},{'id':2}]]` 调用 PG `iter_rows()`，实际结果仍为 `[[{'id':1},{'id':2}]]`。PG 迁移实现 `iosql/engines/postgresql/table.py:1264-1269` 又依赖其批列表语义。
- 影响：同一公共方法跨引擎返回形状不同；按文档逐行处理 PG 数据的代码会收到 `list`。
- 建议：PG 增加显式 `iter_batches()`，把 `iter_rows()` 改为逐行，并同步迁移内部调用与测试。

### P1-7 SQLite 表枚举接受任意子目录，读取会写入该目录（实测）

- 位置：`iosql/database.py:602-610` 只检查目录且不以 `__` 开头；`Database.table()` 随后初始化 `Table`，`iosql/engines/sqlite/table.py:392-403` 打开/创建元数据库。
- 证据：在空 `.iosql` 目录下放 `notes/readme.txt`，`db.table_names()` 返回 `['notes']`；随后只调用 `db.table('notes')`，该目录新增 `meta.sqlite3` 及 WAL/SHM 文件。
- 影响：CLI/Web 把普通目录展示为表；标称只读的发现/打开流程会改写普通目录，并污染后续库识别。
- 建议：枚举时校验 `<table>/meta.sqlite3` 文件头及必要 manifest；读路径不要创建不存在的表元数据。注意与外部创建新表的懒初始化兼容。

### P1-8 PostgreSQL 表枚举固定 `public`，与引擎 schema 解析不一致（静态，待 live PG）

- 位置：`iosql/database.py:583-600` 的 `table_names()` 固定 `n.nspname = 'public'`；引擎自身在 `iosql/engines/postgresql/table.py:224-232` 用 `current_schema()` 解析当前 schema。
- 影响：连接配置 `search_path` 指向非 public schema 时，表对象可按引擎规则访问，但 `Database.table_names()` 和依赖它的 CLI/Web 枚举会漏表，或列出无关 public 表。
- 建议：两处统一 schema 解析策略，并加非 public `search_path` 的 live PostgreSQL 集成用例；当前环境无 `POSTGRES_DSN`，未做 live 复现。

## P2：输入与文档契约

| 编号 | 问题与证据 | 建议 |
| --- | --- | --- |
| P2-1 | `iosql/database.py:295-297` 的 `db.table(spec=contract)` 会用方法默认 `force_partition=False` 覆盖 `contract.force_partition=True`。临时库实测输入为 True，`_declared_spec` 与 `spec()` 都变 False。 | 让 `force_partition` 快捷参数可区分“未传”与显式 False，或拒绝冲突而非静默改写。 |
| P2-2 | `iosql/web/app.py:367-375` 对 `order_by=","` 生成空 `segments` 后取 `segments[0]`，`POST /rows` 返回 500（已实测）；多余 token 也未完整校验。 | 在索引前校验恰好一个非空列名和至多一个方向词，返回 422。 |
| P2-3 | `README.md:400` 仍写 `select()` 默认行元组，而 SQLite 签名 `iosql/engines/sqlite/table.py:3552-3558` 默认 `to_dict=True`，PG 同样默认 dict。 | 修正文档，避免用户按错误返回类型编写代码。 |
| P2-4 | CLI `iosql web --host` 接受非回环地址并提示服务会暴露（`iosql/cli.py:2080-2114`），但 `create_app()` 的 TrustedHost 仅允许回环 Host（`iosql/web/app.py:1393-1397`）。临时客户端以 `Host: 192.0.2.5` 请求返回 400。 | 明确该参数仅支持本机绑定，或提供经过明确授权的 Host 白名单配置；保留 DNS rebinding 防护。 |

## 各模块审查记录与保留风险

| 模块 | 本轮检查与判断 |
| --- | --- |
| 公共 API、`spec.py`、`database.py` | 核对声明/绑定、迁移与重命名入口；发现 P1-2/P1-7/P1-8、P2-1。`TableContract`、`PartitionSpec` 的旧问题已有 PLAN-062 回归。 |
| 分区、`core/query` | 对照上轮修复和现有测试核对 interval、calendar fmt、Case 与分区裁剪；未发现需重新打开的上轮 P0。 |
| SQLite 引擎、`utils/mysqlite` | 全量测试与关键临时库探针通过；发现 P0-2、P1-1/P1-3。PLAN-062 结果仍明确保留 `latest()/asof()` 全量物化、无 segment 布局的全表聚合及 `iter_batches` OFFSET 在并发修改下可能重复/漏行。 |
| PostgreSQL 引擎 | 阅读 DDL、事务、迁移、聚合与枚举接口，运行 mock 测试；发现 P1-3/P1-6/P1-8。没有 live PostgreSQL，所以事务/DDL/分区边界只能由 mock 与静态证据支持，不能声称线上已验证。 |
| CLI | 审查备份/恢复、重命名、迁移等破坏性命令与现有 PLAN-062 用例；发现 P0-3。`web --host` 契约见 P2-4。 |
| Web 后端与前端 | TestClient 临时库验证分页和图表；非 vendor JS 语法通过；发现 P1-4/P1-5、P2-2。浏览器交互与可视区缩放本轮未做真实浏览器回归，PLAN-071 静态契约测试通过。 |
| 构建、文档、示例、benchmark | wheel 隔离安装发现 P0-1；README 漂移见 P2-3。未找到 LICENSE、CI 配置或 Python 3.9 至最新版测试矩阵；benchmark 脚本未用于本轮性能结论。`HANDOFF.md` 仍描述 8 月的 PLAN-054 未提交状态，和当前 HEAD 不符。 |

## 建议修复顺序与验收

1. **发布前**：修 P0-1，加入 wheel/sdist 产物级冒烟；修 P0-2，所有公开重命名入口共享回滚实现，故障注入证明目标数据保留；修 P0-3，恢复前校验备份内容与完整性。
2. **数据正确性**：定义分区表主键作用域并修 P1-1；修图表边界与时间顺序（P1-3/P1-4）；修无主键分页（P1-5）。每项用本文最小数据集写回归。
3. **跨引擎契约**：修直接重命名缓存、PG `iter_rows` 与 schema 枚举（P1-2/P1-6/P1-8），在 live PG 环境补一轮迁移/事务/分区验证。
4. **收尾**：修 P1-7 与 P2 项；同步 README、skill、设计和过期 HANDOFF；建立 lint、构建产物及多 Python 版本门禁。

本文是审查报告，未修改业务实现；每条运行时缺陷的探针均在临时目录执行。已有 723 个通过的测试不能覆盖上述新增场景，修复后应将这些探针转为长期回归。
