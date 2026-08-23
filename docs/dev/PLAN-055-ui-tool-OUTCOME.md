# PLAN-055 · iosql UI 可视化管理工具 · 实施结果

- 日期：2026-08-23
- 计划：docs/dev/PLAN-055-ui-tool.md
- 设计：docs/design/ui-20260823-overview.md
- 状态：实现完成，review 循环进行中

---

## 1. 交付清单

### 后端（Phase A）

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `iosql/ui/__init__.py` | 29 | PEP 562 惰性导出 create_app；未装 fastapi 时给安装指引 |
| `iosql/ui/app.py` | ~1000 | FastAPI 工厂：mode 条件路由、lifespan 单例 Database、15 只读端点 + 7 写端点、chart 两遍流式降采样、export 流式导出 |
| `iosql/ui/serializers.py` | 105 | 三级防御式序列化（to_dict → dataclass 字段白名单 → str 兜底） |
| `iosql/ui/deps.py` | 25 | require_write_mode 纵深防御守卫（404 语义） |
| `iosql/ui/errors.py` | 26 | AppError + {detail, hint} 统一错误体 |

### 前端（Phase B）

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `static/index.html` + `app.js` | 248+入口 | hash 路由 SPA 骨架、spec 缓存、模式徽章 |
| `static/js/tab-overview.js` | 96 | 全库概览卡 + 表清单 |
| `static/js/tab-browse.js` | 255 | 条件构造器 + keyset 翻页表格 + 导出 |
| `static/js/tab-structure.js` | 254 | 契约卡 / 分区 chips / segment 表三层透视 |
| `static/js/tab-query.js` | 227 | 查询 + explain 裁剪可视化 + timeseries 快捷区 |
| `static/js/tab-charts.js` | 267 | uPlot 折线 / OHLC 四线近似 K线 |
| `static/js/tab-maintain.js` | 458 | 维护步骤流（仅 --write 可用）+ 手输表名确认 |
| `static/js/components.js` / `util.js` | 363/288 | h() DOM 构建（textContent 防注入）、api/toast 封装 |
| `static/style.css` | 497 | CSS 变量设计 token、暗色主题 |
| `static/vendor/uplot.*` | 52KB | uPlot 1.6.32（MIT）vendored |

### CLI 与打包（Phase C）

- `iosql/cli.py`：新增 `ui` 子命令（--source/--write/--host/--port/--no-browser），惰性导入带安装指引
- `pyproject.toml`：新增 `[ui]` extra（fastapi>=0.110, uvicorn>=0.29）

### 测试（Phase D）

- `tests/test_ui.py`：527 行 29 用例，覆盖只读端点契约、写端点 readonly 下 404、写操作防呆流程（delete 必填 where / truncate 手输表名 / cleanup 两步）、keyset 不重不漏翻页、能力门控、CLI 校验

## 2. 关键技术结论（探针实测）

1. iosql `select()` 支持 `"ts desc"` 方向与多列 order_by（Python 层归并排序）
2. 无连接级只读支持：只读保障 = 路由白名单（结构性不注册）+ require_write_mode 守卫双层
3. 所有结果对象自带 `to_dict()` 且 JSON 安全
4. 无 time 契约表调 query()/latest() 抛 MissingTimeColumn → 服务层前置 409 门控
5. `db.table(未知名)` 返回 ghost 对象 → 必须先 table_names() 校验存在性
6. calendar 分区自动反推 time 契约（unit='date'），因此普通 calendar 表也有时序查询能力

## 3. 浏览器冒烟发现并修复的集成缺陷（7 处）

| # | 缺陷 | 修复 |
| --- | --- | --- |
| 1 | Overview 分区数显示 ∅（前端读 partition_count，后端发 part_count） | pick 列表加入 part_count |
| 2 | Structure 契约卡全空（前端缓存整个响应信封未解包 spec） | app.js 解包 resp.spec |
| 3 | 契约卡 PK 空（前端读 spec.pk，后端按契约发 primary_key） | 前端改读 primary_key |
| 4 | PARTITION 显示原始 JSON 不友好 | 优先显示规范 str 表达式 |
| 5 | chart 对 calendar 日期字符串 x 轴 500（str 减法崩溃） | x 轴数值化层：纯数字字符串转 int，其他文本明确 422 |
| 6 | 文本列 start/end 整数边界静默失效（SQLite TEXT>INTEGER 亲和性） | 边界值按列类型下发字符串 |
| 7 | Charts 恒显"暂无数据"（normalizePoints 未处理契约的 x/y 平行数组形态） | 契约形态优先分支 |

## 4. 测试结果

| 套件 | 结果 |
| --- | --- |
| tests/test_ui.py | 29 passed |
| tests/ 全量（除 partition_integration） | 332 passed, 16 skipped |
| tests/test_partition_integration.py（无并发复跑） | 42 passed |
| **合计** | **374 passed, 0 failed** |

> 注：首轮全量出现的 partition_integration 失败经基线对照与无并发复跑确认，
> 为多个测试进程并发碰撞共享 /tmp 路径所致，非代码缺陷。

## 5. Review 循环记录

### Round 1（主线集成审查）

- lsp_diagnostics：0 error
- 修复：serialize_partition 导入缺失、spec 端点缺 name 字段、Pyright reportOptionalSubscript、移除 2 处 type: ignore
- 结论：通过

### Round 2（Oracle 后端深度审查）

（待 Oracle 结果补充）

### Round 3（最终干净浏览器冒烟）

（待前端代理结束后执行）

## 6. 已知限制与后续增强

1. candle 图对非 OHLC 表返回 422+hint（前端 toast 引导切换 line）；真正的蜡烛图渲染为 v2
2. repartition 无服务端 dry-run（repartition_to API 不支持），以确认弹窗替代；CLI --dry-run 的计算型预览列为增强项
3. ISO 格式日期字符串（"2026-01-05"）作为 chart x 轴不支持（仅 YYYYMMDD 纯数字串），文档已注明建议 duration 时间戳分区
4. 严格只读（sqlite mode=ro 直读 meta.sqlite3，零 WAL 触碰）列为 v2 增强
5. import_buffer（multipart 批量导入文件）未纳入 v1 写端点（契约冻结为 7 个写端点）

## 7. 使用方式

```bash
pip install "iosql[ui]"
iosql ui -s database/market.iosql            # 默认 READ-ONLY
iosql ui -s database/market.iosql --write    # 开启维护操作
```
