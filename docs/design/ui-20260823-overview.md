# iosql UI 可视化管理工具 · 设计总览

- 日期：2026-08-23
- 状态：定稿（三项调研完成：技术选型 / iosql API 面 / DB Browser 设计分析）
- 参考：DB Browser for SQLite（sqlitebrowser）、datasette、sqlite-web
- 关联计划：docs/dev/PLAN-055-ui-tool.md

---

## 1. 背景与目标

iosql 是面向金融时序数据的分区式 SQLite 存储库，当前交互手段是 Python API 和 CLI。两类方式都要求用户熟悉 API 形态，缺少"打开就看、点了才动"的可视化入口。

**目标**：为 iosql 提供一个随库分发的本地 Web 管理工具：

1. **零门槛观察**：不写一行代码即可查看表、契约、分区、segment、数据样本。
2. **双模式安全**：只读模式（默认）杜绝一切写操作；可修改模式显式开启，危险操作二次确认。
3. **时序友好**：围绕金融场景提供时间范围筛选、最新截面、价格曲线/K线快览。
4. **随库分发**：`pip install "iosql[ui]"` + `iosql ui -s <db>` 一条命令启动，不污染核心依赖。

**非目标**（Non-goals）：

- 不做通用 SQL 编辑器（iosql 的查询语义由 where 构造器表达，不做裸 SQL 注入面）。
- 不做多用户/远程部署安全模型（定位本机 localhost 工具）。
- 不做数据可视化分析平台（图表仅做"快览"，重分析交给专业工具）。
- 不替代 CLI 运维脚本化能力（UI 是人肉排查入口，CI/自动化仍走 CLI）。

---

## 2. 真实用户场景分析

### P1 数据工程师 —— "数据进对了没有"

> 场景：刚跑完 20 年日线回灌或当晚 tick 采集，入库后要确认质量。

- 痛点：现在只能 `iosql spec/layout/segments` 逐命令敲，肉眼对比 JSON 输出。
- 需求：
  - 一眼看全库概览：每张表行数、分区数、占用大小、时间范围。
  - 分区分布健康度：哪些分区行数异常（空分区/超肥分区）。
  - 抽样浏览任意分区数据，核对字段格式。
  - `validate(deep=True)` 一键体检，错误列表可视化。
- 模式需求：**只读足够**（90% 时间不需要写）。

### P2 量化研究员 —— "这批数据能不能用"

> 场景：写策略前探索数据：某 symbol 价格走势、某交易日 tick 密度、复权因子覆盖范围。

- 痛点：为了"看一眼"要写临时 Python 脚本。
- 需求：
  - 按 symbol / 时间窗筛选数据表格，排序浏览。
  - 选中数值列一键画折线/K线快览（服务端降采样，秒级出图）。
  - `latest/asof` 最新截面查询：所有 symbol 截至 T 日的收盘价快照。
  - 筛选结果导出 CSV/JSONL（浏览器下载流）。
- 模式需求：**只读足够**（导出是读操作的延伸）。

### P3 存储运维者 —— "磁盘为什么涨这么快"

> 场景：库目录从 50GB 涨到 300GB，或查询突然变慢，怀疑布局劣化。

- 痛点：compact → validate → cleanup_obsolete 是一条有顺序、有风险的流水线，CLI 敲错代价高。
- 需求：
  - `layout_report` 可视化：分区行数分布图 + 官方 recommendation 直接展示。
  - segment 清单：文件 key、行数、min/max stats、active/sealed/obsolete 状态、可回收字节。
  - 维护操作台（**需可写模式**）：compact 预览 → 执行；cleanup_obsolete 先 dry-run 显示 bytes_reclaimable，确认后才真删。
  - repartition 确认弹窗（目标 URI + 新分区串 + "源库不变"声明，需手输表名）后执行；服务端无原生 dry-run，不做假预览（估算预览列为增强项）。
- 模式需求：**必须可修改模式**，且每步有预览-确认节奏。

### P4 iosql 库开发者 —— "裁剪到底生效没有"

> 场景：开发/调试期验证二级裁剪、晚到数据进 delta segment、session 列编译结果。

- 需求：explain_select 可视化——候选 vs 实际扫描的 partitions/segments、skip_reasons 逐条列出。
- 模式需求：只读。

**结论**：四类场景中三类完全只读，一类需要受控可写。→ 双模式设计的核心依据。

---

## 3. 参考 DB Browser for SQLite 的取舍

DB Browser 的信息架构是平级 Tab：Database Structure / Browse Data / Edit Pragmas / Execute SQL，左侧无表树（单文件库通常表少）。它证明了"平级职责 Tab + 表格为中心"的心智模型对 SQLite 用户有效。

**借鉴**（含 DB Browser 调研确认的机制）：

| DB Browser 设计 | iosql UI 采用 |
| --- | --- |
| Structure / Browse Data / Execute SQL 三 Tab + 左侧结构 Dock | 左侧表列表 + Overview/Browse Data/Structure/Query/Charts/Maint 平级 Tab |
| Browse Data 块预取（默认 256 行）+ 后台线程加载 + 状态栏提示 | keyset 分页（服务端 200 行/页），异步加载 + 加载态指示 |
| 列头过滤输入框 + 全局过滤；用户抱怨"只支持 AND 不支持 OR"是高频缺口 | where 构造器**从第一天就支持 and/or 组合**（iosql Case 原生支持） |
| 打开时勾选 Read-only / CLI `-ro`（v3.13+ 才有，issue #3760 拖了多年） | 启动参数 `--read-only` 为**默认值**，`--write` 显式开启（更保守：金融库误写代价高） |
| 危险操作确认弹窗；结构修改合并到 OK 一次执行 | truncate/cleanup_obsolete(实删)/repartition 强制确认 + 影响预估文案 |
| v3.12+ Plot Dock 定位 "quick assessment 非专业图表"；折线/阶梯/散点、双 Y 轴、对数轴 | Charts Tab 同定位：折线/K线快览 + 对数轴开关，不追求专业绘图 |
| 用户抱怨高频项：千分位缺失、auto-apply 编辑选项、快捷键不足 | 吸收为体验要求：数值千分位显示、筛选即时生效、常用快捷键（翻页/刷新/切 Tab） |

**差异**（不照搬的理由）：

1. iosql 是**目录库**（一逻辑表多物理文件），左侧必须有**表列表侧栏**，Structure Tab 要同时呈现三层：契约(spec) → 逻辑分区(parts) → 物理 segments。
2. 不做 Execute SQL 裸 SQL Tab：iosql 查询走受控 where 语法（dict/list/text-expression），开放裸 SQL 既绕过裁剪又引入注入面。Query Tab 用"条件构造器 + explain 可视化"替代。
3. 新增 **Diagnostics Tab**：layout_report / validate / compact / cleanup_obsolete 流水线是 iosql 特有的高价值运维面，通用 SQLite 工具没有对应物。

---

## 4. 技术选型

### 结论：本地 Web 方案（FastAPI + 无构建链静态前端）

| 维度 | Web（选定） | PyQt/PySide6（备选） |
| --- | --- | --- |
| 分发 | `pip install "iosql[ui]"` 即用 | PyInstaller 打包痛点（资源路径/DLL/杀软误报） |
| 依赖哲学 | fastapi+uvicorn 纯 Python，可控 | Qt 绑定体积大、GPL/LGPL 许可需注意 |
| 大表浏览 | 服务端分页 + 前端虚拟滚动，方案成熟 | QTableView fetchMore 可行但代码量大 |
| 时序图表 | uPlot（47KB，初始渲染 34ms）满足快览 | pyqtgraph 性能强但绑定桌面 |
| 开发效率 | HTML/CSS/JS 单页，无构建链 | Model/View 架构样板代码多 |
| 调研佐证 | datasette/sqlite-web 证明 Python+Web 路线可行 | 无活跃的现代 PyQt SQLite GUI 项目超过 DB Browser |

**关键决策记录**：

1. **不用 NiceGUI**：虽然开发更快，但它拖入大型依赖树（Vue/Quasar 打包资源），版本迭代快存在 API 漂移风险，与 iosql 极简依赖哲学冲突。FastAPI/uvicorn 已是环境常客，增量成本最低。
2. **不用裸 React/Vue 构建**：引入 node 工具链违背"零构建链"原则。采用 vanilla JS + fetch + vendored uPlot（单文件 47KB，MIT 许可，随包分发静态资源）。
3. **PySide6 记录为未来选项**：若未来需要离线原生窗口体验，可用 pywebview 包裹同一 FastAPI 后端（native mode），架构不需推翻。

### 技术栈明细

- 后端：FastAPI + uvicorn（`[project.optional-dependencies] ui = ["fastapi>=0.110", "uvicorn>=0.29"]`）
- 前端：单 HTML 入口 + vanilla JS 模块 + uPlot（vendored 于 `iosql/ui/static/vendor/`）
- 服务形态：uvicorn 起 127.0.0.1 随机/指定端口，自动开浏览器（`--no-browser` 可关）
- 安全边界：默认绑定 loopback；只读模式下服务层不注册任何写端点（404 而非 403，彻底消除误调用面）；服务端用 TrustedHost 只信任 `localhost`/`127.0.0.1`/`[::1]` 的 Host 头（DNS rebinding 纵深防御），CLI 绑定非 loopback `--host` 时打印暴露警告。

---

## 5. 核心设计：只读模式 vs 可修改模式

### 5.1 模式语义

| | 只读模式（默认） | 可修改模式（`--write`） |
| --- | --- | --- |
| 定位 | 观察/探索/导出 | 数据修正与布局维护 |
| 进入方式 | `iosql ui -s DB` | `iosql ui -s DB --write` |
| UI 提示 | 头部绿色徽章「READ-ONLY」 | 头部橙色徽章「WRITABLE」+ 写操作按钮可见 |
| 服务端保障 | 写类路由**不注册**（请求 404） | 注册且执行前置检查 |

**实现要点**：只读不是前端隐藏按钮（那是化妆），而是 FastAPI 按 mode 条件挂载路由——只读进程里根本不存在写端点。这是结构性隔离，审计一眼可信。

### 5.2 操作分类矩阵（API 白名单依据）

**只读集合（两种模式都可用）**：

| UI 功能 | iosql API |
| --- | --- |
| 全库概览 | `db.table_names()` + 每表 `count_rows()`（懒加载缓存） |
| 表契约 | `table.spec()` |
| 逻辑分区 | `table.table_parts()` |
| 物理段清单 | `table.segments(include_obsolete=...)` |
| 数据浏览/筛选/排序 | `table.select(where=..., order_by=..., limit, to_dict=True)` |
| 行数统计 | `table.count_rows()` |
| 查询计划 | `table.explain_select(...)` |
| 最新截面 | `table.latest(symbols, as_of, by, columns)`（timeseries 表） |
| 时间窗查询 | `table.query(start/end/date/session)`（timeseries 表） |
| 布局报告 | `table.layout_report()` |
| 一致性校验 | `table.validate(deep=...)` |
| 导出 | `iter_select_batches` → 流式 CSV/JSONL 下载 |

**可写集合（仅 `--write` 模式注册）**：

| UI 功能 | iosql API | 防呆措施 |
| --- | --- | --- |
| 追加写入行 | `table.write(rows)` | JSONL 粘贴框 + 逐行校验预览 |
| 条件删除 | `table.delete(where)` | 必填 where + 先 `count` 预览命中行数 + 输入表名确认 |
| 清空表 | `table.truncate(confirm=True)` | 二次弹窗 + 手输表名 |
| 建索引 | `table.create_index(column)` | 显示涉及分区数 |
| 整理 segment | `table.compact(partition=None)` | 展示 rows_read/written/new_segments 结果 |
| 清理废弃段 | `table.cleanup_obsolete(dry_run)` | **强制两步**：dry-run 预览 bytes_reclaimable → 显式确认删除；执行前内部先 validate(deep=True) |
| 重分区 | `table.repartition_to(target_uri, spec)` | 目标 URI 必填（永不原地改源库）；`repartition_to` 无原生 dry-run，UI 以确认弹窗展示目标 URI + 新分区串替代预览（CLI 的 `--dry-run` 为计算型预览，v1 不复制该逻辑，列为增强项） |

**明确排除在 UI 之外**（保持走 CLI/API）：rename-database、migrate_to/to_postgresql、backup/restore——低频重量级操作，UI 化收益低风险高。

### 5.3 只读的技术纵深（防御分层，已按代码调研校准）

> 代码事实（explore 确认）：iosql 全库**不存在连接级只读支持**——所有连接经
> `connection_pool` 以读写模式 `sqlite3.connect(...)` 打开并执行 pragma（默认 WAL/NORMAL），
> 即使纯查询也可能触碰 WAL 文件。"只读"目前只是 API/命令语义划分。

因此本工具的只读保障分层如下：

1. **路由层（主防线）**：mode=read-only 时 FastAPI 不注册任何写路由——写端点在只读进程中不存在（404），而非被拒（403）。结构性隔离，审计一眼可信。
2. **服务层守卫（纵深）**：统一 `require_write_mode()` FastAPI Depends，防未来误挂路由。
3. **文档如实声明**：只读模式承诺"UI 无任何数据修改入口"；底层连接由 iosql 以默认方式打开（可能产生 WAL 副文件），不承诺文件级 mtime 不变。
4. **前端体验层**：写控件整体隐藏 + 徽章提示（不作安全保障）。
5. **未来增强（v2 备选）**：严格只读模式绕过 iosql API，直接 `sqlite3.connect("file:...?mode=ro", uri=True)` 读 `meta.sqlite3` 的 `meta`/`table_part`/`_iosql_segments` 表与 `data/*.sqlite3`——元数据 schema 已探明（const.py），完全可行，作为库的能力缺口补齐。

---

## 6. 信息架构

```
┌────────────────────────────────────────────────────────────────┐
│ iosql UI   [db路径 ▾]  ticks · daily_bars · …   ● READ-ONLY    │
├────────────┬───────────────────────────────────────────────────┤
│ 表列表      │ [Browse Data] [Structure] [Query] [Charts] [Maint] │
│ ─────────  │                                                    │
│ ▸ ticks    │  （当前表的主工作区，Tab 平级切换）                  │
│   12.4M行  │                                                    │
│ ▸ daily    │                                                    │
│   25M行    │                                                    │
│ ▸ events   │                                                    │
└────────────┴───────────────────────────────────────────────────┘
```

### Tab 1 · Overview（库级总览，选中"全部"/首屏）

- 卡片行：表数量 / 总行数（懒加载）/ 总大小（目录统计）。
- 表清单表：表名、行数、分区数、segment 数、partition 策略摘要、时间范围(min/max)。

### Tab 2 · Browse Data（对标 DB Browser Browse Data）

- 工具栏：where 条件构造器（field 下拉[来自 spec.columns] + op 下拉[==/!=/>/>=/</<=/between/in] + value 输入，多条件 and/or）、order_by 列选择、limit。
- 表格：服务端分页（页大小 200），列头点击排序；单元格点击侧滑查看完整值（长文本/JSON 友好显示）。
- 底部：总行数（首次进入异步拉 count_rows，带缓存标记）、当前页码。
- 导出按钮：当前筛选条件 → CSV/JSONL 浏览器流式下载。

### Tab 3 · Structure（三层结构透视，iosql 特色）

- 区块 A 契约卡：pk、columns(类型)、partition 策略串、segment 配置、order_key/time/session_column、conflict。
- 区块 B 逻辑分区表：part key、行数、segment 数、时间范围；行数迷你条形图直观看偏斜。
- 区块 C 物理段表：file_key、logical partition、rows、status(active/sealed/obsolete)、stats min/max；开关 include_obsolete。

### Tab 4 · Query（条件查询 + 计划可视化 + 截面）

- 上半区：同 Browse 的条件构造器，附加「Explain」按钮 → 裁剪可视化面板：
  - partitions: 候选 N / 扫描 M / 跳过 K，跳过项逐条 skip_reasons；
  - segments 同构展示（二级裁剪证据链）。
- timeseries 表专属快捷区：start/end/date/session/symbols 表单 → query 结果表；latest/asof 表单（symbols、as_of、by、columns）→ 截面表。

### Tab 5 · Charts（时序快览）

- 配置行：symbol(s) 多选、数值列、时间范围、图型（line / candlestick 若识别 OHLC 列名）。
- 实现：服务端按可视像素宽度分桶降采样（≤4000 点）返回，uPlot 渲染。
- 多 symbol 叠加对比开关。

### Tab 6 · Maintain（诊断维护台，仅 `--write` 可见可操作）

- 步骤流 UI（引导正确顺序）：layout_report → compact → validate(deep) → cleanup_obsolete(dry-run → 确认删除)。
- repartition 面板：新 partition 串输入（带 duration/calendar/range 语法提示）+ target URI + 执行按钮（按钮为「执行重分区（写入新库）」；confirm 弹窗声明"写入新库，源库不变"并需手输表名确认；无服务端 dry-run，不提供假预览）。
- 所有执行类按钮：确认弹窗内回显将执行的 iosql 语义说明（如"将在新库 X 创建 hourly 布局，源库不变"）。

---

## 7. 后端 API 面（REST 草案）

> 分页关键约束（代码调研确认）：`table.select()` **没有 offset 参数**，只有 `where + order_by + limit`。
> 因此 Browse Data 采用 **keyset 分页**：游标为复合键 `[排序列值, 主键值...]`，
> 条件展开为 `(c1>v1) OR (c1==v1 AND c2>v2)` 的 OR 链——排序列有并列值
> （多 symbol 同 tick）时不会跳行。正确性由 keyset 保证。排序列一旦含 NULL，
> keyset 无法表达 IS NULL（首页探测即返回 422 并提示更换排序列），前端禁用该列
> 排序，避免"看起来翻完了其实漏行"。核心侧 `select(order_by, limit)` 已把
> `ORDER BY ... LIMIT` 下推到每个 segment 再归并，深翻页内存与全量物化解耦。

```
GET  /api/db                          # uri、模式、表汇总（懒加载统计）
GET  /api/tables                      # 表清单 + 每表摘要（含 spec 摘要：是否有 time 契约→能力门控）
GET  /api/table/{name}/spec           # TableSpec 契约 JSON
GET  /api/table/{name}/parts          # 逻辑分区
GET  /api/table/{name}/segments?include_obsolete=
POST /api/table/{name}/rows           # body: {where[], order_by, limit, cursor?} → 行 + next_cursor
POST /api/table/{name}/explain        # body: 同上 → QueryPlan
POST /api/table/{name}/count          # body: {where?} → 无 where 走 manifest 求和；带筛选流式累计，超 5,000,000 行扫描预算 422
POST /api/table/{name}/query          # timeseries 快捷(start/end/date/session/symbols)
POST /api/table/{name}/latest         # as_of/by/columns/symbols（仅 spec.time 存在时可用）
GET  /api/table/{name}/chart          # ?col=&symbol=&start=&end=&buckets= → 服务端降采样序列
GET  /api/table/{name}/export         # ?format=csv|jsonl&where=... → StreamingResponse 流式下载
GET  /api/table/{name}/layout_report
POST /api/table/{name}/validate       # {deep}

# ---- 以下仅在 --write 模式注册 ----
POST /api/table/{name}/write          # {rows:[...]}
POST /api/table/{name}/delete         # {where} （where 必填）
POST /api/table/{name}/truncate       # {}（body 需 confirm:name）
POST /api/table/{name}/create_index   # {column}
POST /api/table/{name}/compact        # {partition?, target_rows?}
POST /api/table/{name}/cleanup_obsolete # {dry_run, partition?}
POST /api/table/{name}/repartition    # {target_uri, partition_str}（无服务端 dry-run；前端手输表名确认后直接写入）
```

约定：

- 错误统一 `{detail: str, hint?: str}`；写操作响应附 iosql 原生结果对象（WriteResult/CleanupResult 等 `to_dict()`）。
- 写请求体模型 `extra="forbid"`：未知字段（如已移除的 `dry_run`）一律 422，不会被静默吞掉。
- **能力门控**：`spec().time` 存在的表才启用 Query Tab 的 start/end/date/session/latest/asof 区块与 Charts 的 candlestick；普通表只保留 where 构造器查询。前端从 `/api/tables` 摘要读取能力位。
- 后端取表引用必须用裸 `db.table(name)`（禁止重传契约参数，避免 TableSpecConflict）。
- Database 实例为进程单例（uvicorn workers=1），FastAPI shutdown 时 `db.close()`。

---

## 8. 代码落位（草案，待 explore 结果校准）

```
iosql/
└── ui/
    ├── __init__.py          # lazy import，未装 fastapi 时给安装指引
    ├── server.py            # FastAPI app 工厂 create_app(db_uri, mode)
    ├── deps.py              # require_write_mode 等依赖
    ├── serializers.py       # iosql 结果对象 → JSON 安全 dict
    ├── static/
    │   ├── index.html
    │   ├── app.js           # vanilla JS SPA（模块拆分视体量而定）
    │   ├── style.css
    │   └── vendor/uplot.min.js (+ .css)
cli.py / iosql/cli.py        # 新增 `iosql ui` 子命令
tests/test_ui_*.py           # httpx TestClient 覆盖
pyproject.toml               # [ui] extra
docs/design/ui-20260823-overview.md
docs/dev/PLAN-{N}-ui-tool.md
```

---

## 9. 风险与开放问题（全部已决）

| # | 问题 | 结论 |
| --- | --- | --- |
| R1 | `Database(uri)` 打开已有库是否有隐式写？ | **已确认**：无连接级只读支持，纯查询也可能触碰 WAL。防御止步于路由白名单+服务守卫，文档如实声明；严格只读直读 meta.sqlite3 列为 v2 增强 |
| R2 | count_rows 对超大表延迟 | UI 懒加载 + 进程内缓存（TTL），Overview 首屏异步补齐 |
| R3 | chart 降采样桶聚合形态 | 用 iter_select_batches 流式读取 + Python 层分桶（≤4000 桶返回） |
| R4 | DB Browser 交互细节校准 | **已完成**：块预取分页、Plot 快览定位、OR 过滤缺口、危险操作合并执行等均已吸收进第 3/5 节 |
| R5 | uPlot 许可(MIT)与 vendored 分发 | 已下载 uPlot 1.6.32（51KB js + 1.8KB css，MIT）随包分发，合规 |

## 10. 实施摘要

详见 docs/dev/PLAN-055-ui-tool.md。要点：

1. 后端 `iosql/ui/`：FastAPI app 工厂 + mode 条件路由注册 + 序列化层；CLI `iosql ui` 子命令。
2. 前端 `iosql/ui/static/`：零构建链 vanilla JS SPA + vendored uPlot。
3. 测试：httpx TestClient 全端点覆盖（read-only 404 断言、keyset 翻页正确性、写模式防呆流程）。
4. pyproject 新增 `[ui]` extra：fastapi、uvicorn。
