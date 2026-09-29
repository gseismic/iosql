# iosql Web 最终 Review 报告（2026-09-29）

## 审查范围与结论

审查覆盖 Web 路由和序列化、SQLite/PostgreSQL 查询迭代改动、SPA 的 Data/Charts/Maintain 页面、配套 README 与使用指南；并用真实 Binance 分钟 K 线指标库做 Charts 浏览器验收。

初审记录的 6 项 P2 行为问题和 1 项 P3 文档问题已完成修复和复查。图表初审发现的“2,000 桶均值、跨 symbol 混算及合成 x 坐标”也已由 PLAN-074 改为原始点流。追加取消场景复查时还发现并修复了 SQLite 连接跨线程清理异常。当前复查未发现上述范围内尚未关闭的功能错误。

本次没有运行全仓测试套件。回归测试结果、语法检查及浏览器实测范围见本报告后文。实施记录分别见 `docs/dev/PLAN-073-web-review-fixes-OUTCOME.md` 和 `docs/dev/PLAN-074-chart-raw-points-OUTCOME.md`。

## 初审问题及处理状态

| 编号 | 严重度 | 初审发现 | 复查状态 |
| --- | --- | --- | --- |
| P2-1 | P2 | Maintain 清理确认没有锁定 dry-run 时的分区范围 | 已修复。预览保存 partition 快照；输入范围变化时禁用删除，实际请求仅使用通过校验的快照。 |
| P2-2 | P2 | `/count` 预算按命中行数计算，不能限制低选择性查询工作量 | 已修复。筛选计数改用 5 秒查询期限；SQLite 中断执行，PostgreSQL 使用 statement timeout，超时返回可恢复的 422 提示。 |
| P2-3 | P2 | 切表期间旧 `/spec` 响应可能污染新表缓存或界面 | 已修复。缓存键使用请求开始时捕获的表名，只有表仍为当前表时才渲染或显示错误。 |
| P2-4 | P2 | 图表把多个 symbol 混成桶均值且 x 使用合成桶中点 | 已修复并扩展。Charts 现在对每个 symbol 返回完整原始点和原始 x，不分桶、不抽样。 |
| P2-5 | P2 | Explain 没有使用 Data 当前页请求的 limit | 已修复。Explain 对 limit 使用与 rows 相同的边界并将其传给查询计划。 |
| P2-6 | P2 | JSONL 可输出 NaN/Infinity，特殊值表示不统一 | 已修复。行值统一走 `to_jsonable()`，JSON 使用 `allow_nan=False`。 |
| P2-7 | P2 | 浏览器中止图表流后，SQLite 有序读取器可能在不同工作线程关闭连接并抛 `ProgrammingError` | 已修复。只读串行流连接允许跨工作线程恢复和关闭；新增跨线程关闭回归测试，并以真实浏览器取消全库流复验，服务端日志干净。 |
| P3-1 | P3 | Web 指南入口、安装 extra 和工作区列表过期 | 已修复。更新为 `iosql.web`、`.[web]` 和当前 Overview/Data/Structure/Charts/Maintain 工作区。 |

详细初审描述仍保留在 `docs/review/web-review-20260929-followup.md`；该文档顶部注明它是发现时的状态快照。

## Charts 真实数据复查

测试库：`/home/lsl/Z1/findata/crypto/binance/future_usdt/kline.factor.iosql`。表 `kline_factor_1m` 有 4,183,200 行、29 列和 8 个 symbol，数据时间从 2022-01-01 00:00 UTC 到 2022-12-31 23:59 UTC。

### 数据完整性与直观检查

- 原实现默认只返回 2,000 个桶均值；空 symbol 时把八种资产混成一条线，且 x 是桶中点。这会抹平分钟级变化并产生无金融含义的跨资产均值。
- 修复后，全范围请求返回 4,183,200 条记录、8 条独立序列。ADA/BNB/BTC/DOGE/ETH 各 525,600 点，LTC/SOL/TRX 各 518,400 点，与源表行数一致。首尾时间轴对应源表真实首尾 `open_time`。
- BTCUSDT RSI 全年 525,600 点；源值范围约 3.09–97.28，均值约 49.96。实图数值落在合理的 RSI 区间，细节窗口可见分钟级起伏，没有混入其它币种的均值。
- 最新前端下的一天 RSI 多币种查询返回 11,520 点：八条线各 1,440 点。浏览器状态文案明确标出「无抽样」及逐线点数。
- 拖拽缩放到约 4.8 天返回 205,147 点；缩小到局部窗口返回 6,903 点；双击复位重新取回全年 525,600 点。查询窗口只限制数据范围，不降低窗口内点数。
- 浏览器控制台在复验窗口为 0 errors、0 warnings；接口请求均为 HTTP 200。
- 在全库多 symbol RSI 流仍加载时改画 BTC 单日窗口，确认旧流可取消；新窗口返回 1,440 点，浏览器和服务端均无取消清理异常。

### 性能与视觉边界

- 全范围八标的加载耗时约 34.5 秒、网络数据约 126 MB；BTC 单标的全年约 4.18 秒、约 18 MB。完整点数组在浏览器中的观测内存约 56 MB。
- SMA/EMA/ATR 等价格量纲的多币曲线共用 Y 轴时，BTC 的价格范围会压缩低价币线；这是同轴显示造成的视觉差异，不是记录被省略或值被平均。界面已给出提示，建议此类指标单标的查看。RSI 等归一化指标适合并列比较。
- 当前 candle 选项显示原始 OHLC 四线，不包含传统蜡烛实体；界面已明确标注。若用户需要传统 K 线烛体，这是尚未实现的展示形态。
- 32 个 symbol 是服务端上限，超限明确报错，不会静默删线。

## 验证记录

- `python -m py_compile iosql/web/app.py tests/test_chart_pushdown.py`：通过。
- `node --check`：`tab-charts.js`、`app.js`、`tab-maintain.js` 通过。
- `python -m pytest -q tests/test_chart_pushdown.py`：25 passed。
- `git diff --check`：通过。
- 浏览器使用 Chromium，服务端以只读模式连接真实数据文件；目标图表请求和缩放/复位请求均返回 HTTP 200。
- 未运行全仓测试套件。

## 相关文件

- 初审发现：`docs/review/web-review-20260929-followup.md`
- PLAN-073 修复记录：`docs/dev/PLAN-073-web-review-fixes-OUTCOME.md`
- PLAN-074 图表实施结果：`docs/dev/PLAN-074-chart-raw-points-OUTCOME.md`
- Charts 设计：`docs/design/chart-20260929-raw-points.md`
