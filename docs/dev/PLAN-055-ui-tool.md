# PLAN-055 · iosql UI 可视化管理工具实施计划

- 日期：2026-08-23
- 设计依据：docs/design/ui-20260823-overview.md（定稿）
- 前置调研：技术选型（Web/NiceGUI vs PySide6 → FastAPI+vanilla JS）、iosql API 面、DB Browser UX 分析

---

## 1. 目标

交付 `pip install "iosql[ui]" && iosql ui -s <db>` 即用的本地 Web 管理台：

1. 只读模式（默认）：路由级白名单，写端点不存在。
2. 可修改模式（`--write`）：写入/维护操作带预览-确认节奏。
3. 五大工作区：Overview / Browse Data / Structure / Query+Charts / Maintain。
4. 全端点测试覆盖 + 全量测试套件通过。

## 2. 工作分解

### Phase A · 后端服务层（`iosql/ui/`）

| 任务 | 内容 | 验收 |
| --- | --- | --- |
| A1 `__init__.py` | 惰性导出；未装 fastapi 时抛带安装指引的 ImportError | 无 fastapi 环境 import 不炸且提示明确 |
| A2 `serializers.py` | dataclass→dict（TableContract/PartitionSpec/Segment/各 Result）；datetime/bytes 安全转换 | JSON 序列化无 TypeError |
| A3 `app.py` | `create_app(db_uri, mode)` 工厂；Database 进程单例 + shutdown close；静态目录挂载；错误统一 handler `{detail,hint}` | TestClient 启动正常 |
| A4 只读路由 | /api/db /tables /spec /parts /segments /rows(keyset) /explain /count /query /latest /chart(降采样) /export(流式) /layout_report /validate | 全部 200 且数据正确 |
| A5 写路由 | write/import_buffer/delete/truncate/create_index/compact/cleanup_obsolete/repartition + `require_write_mode` Depends | read-only 模式 404；write 模式可用 |

关键实现约束：

- 取表引用一律裸 `db.table(name)`；未知表名返回 404 detail。
- keyset 分页：cursor 为上一页末行排序列值；where 组合 `{field: sort_col, op: ">", value: cursor}`。
- chart 端点：iter_select_batches 流式读 + Python 分桶 ≤4000 点；识别 OHLC 列名（open/high/low/close 或 o/h/l/c）时输出 candle 桶。
- export：StreamingResponse，csv/jsonl 二选一；复用同一 where 编码。
- uvicorn 单 worker；线程池并发下依赖 iosql 连接池自身线程安全。

### Phase B · 前端（`iosql/ui/static/`）

| 任务 | 内容 | 验收 |
| --- | --- | --- |
| B1 骨架 | index.html + style.css + app.js（hash 路由 SPA）；头部 DB 路径/模式徽章；左侧表列表（含行数懒加载） | 打开首页可导航全部 Tab |
| B2 Browse Data | 条件构造器(field/op/value + and/or)、keyset 翻页、列排序、单元格详情侧滑、千分位显示、CSV/JSONL 导出按钮 | 大表翻页流畅、筛选正确 |
| B3 Structure | 契约卡 + 分区表(行数条形图) + segment 表(obsolete 开关) | 三层结构完整呈现 |
| B4 Query | 构造器 + Explain 裁剪可视化(候选/扫描/跳过原因) + timeseries 快捷区(start/end/date/session/symbols/latest) | 能力门控生效 |
| B5 Charts | uPlot 折线/K线；symbol 多选叠加；时间范围；对数轴开关 | ≤4000 点渲染 <100ms |
| B6 Maintain | 步骤流(layout→compact→validate→cleanup dry-run→确认删)；repartition dry-run 预览；危险操作手输表名确认 | 仅 --write 可见 |

vendored 资源：uPlot 1.6.32（MIT）js+css 放 `static/vendor/`。

### Phase C · CLI 与打包

| 任务 | 内容 |
| --- | --- |
| C1 `iosql/cli.py` 新增 `ui` 子命令 | 参数 `-s/--source`(必填) `--write` `--host`(默认127.0.0.1) `--port`(默认8686) `--no-browser`；启动前校验 source 存在；打印访问 URL |
| C2 `pyproject.toml` | `[project.optional-dependencies] ui = ["fastapi>=0.110", "uvicorn>=0.29"]` |

### Phase D · 测试（`tests/test_ui_*.py`）

| 任务 | 内容 |
| --- | --- |
| D1 fixtures | tmp_path 建 demo 库：timeseries 表(duration 分区+segment)+普通 calendar 表，写入样本数据 |
| D2 只读模式测试 | 全只读端点 200/数据正确；所有写端点 404；模式徽章字段正确 |
| D3 写模式测试 | write 后 count 变化；delete where 必填校验；truncate 需 confirm:name；cleanup_obsolete 两步流程；repartition dry_run 不落盘 |
| D4 keyset 分页测试 | 写入跨 3 分区数据，逐页翻取不重不漏至耗尽（next_cursor=None） |
| D5 能力门控测试 | 普通 table 的 latest/query 端点返回 409/明确错误 |
| D6 CLI 冒烟 | `--help` 输出；source 不存在时报错退出 |

### Phase E · 收尾

| 任务 | 内容 |
| --- | --- |
| E1 文档 | README 增加 UI 章节；skill 同步（可选）；设计文档回填偏差 |
| E2 Review 循环 | oracle 审查实现 → 修复 → 复审直至零问题 |
| E3 整体测试 | pytest 全量 + 手动冒烟（起服务截图验证五 Tab） |
| E4 结果文件 | docs/dev/PLAN-055-ui-tool-OUTCOME.md + 中文 commit |

## 3. 执行策略

三个独立单元并行委派（API 契约已在设计文档第 7 节冻结）：

1. Agent-backend：Phase A 全部
2. Agent-frontend：Phase B 全部（对照契约开发）
3. Agent-infra：Phase C + D（CLI/pyproject/测试先行于联调）

集成与 E 阶段由主线完成。

## 4. 风险预案

| 风险 | 预案 |
| --- | --- |
| select 多列 order_by 支持不确定 | 排序列单列 + pk 兜底；实测不支持则退化为单列并文档注明 |
| uPlot candlestick 非内置 | 用两条 series(high-low range + open-close) 组合或自绘 candle 插件；v1 保底折线 |
| explain 返回结构字段名差异 | 以运行时 dir() 实测为准，serializers 做防御式 getattr |
| TestClient 生命周期 | 使用 `with TestClient(app)` 触发 startup/shutdown 保证 db.close() |
| **Python 3.9 兼容** | 项目 requires-python>=3.9；ui 包禁用 3.10+ 运行时语法（`X \| Y` 联合类型、match 等），用 `Optional[X]`/`Union` 或 `from __future__ import annotations`。review 清单必查项 |
