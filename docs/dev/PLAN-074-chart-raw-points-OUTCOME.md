# PLAN-074：Charts 全量原始点绘制结果

## 结果

已将 Charts 改为按批次输出并接收所选范围内的全部原始记录。未指定 symbol 时按标的分线；时间、数值空值、缩放窗口和复位均保留原始数据语义。Charts 不再使用分桶均值或点数抽样。

## 代码变更

1. `/api/table/{name}/chart` 改为 NDJSON 流，逐批返回原始 x/y 或 OHLC，并在结尾提供总行数和每个 symbol 的行数；最多 32 个 symbol，超限显式报错，不静默截断。
2. Charts 前端使用扁平数组流式解析，检查服务端总数与逐 symbol 数量；重复 x 值对齐时保留重复次数，null/non-finite y 作为断点。
3. 删除 buckets 控件；状态区显示查询范围、原始总点数、序列数及各序列点数。拖拽缩放重查完整窗口，双击恢复全范围。
4. OHLC 路径同样保留每行的原始 OHLC，不再分桶；前端明确显示为 OHLC 四线近似图，没有蜡烛实体。
5. README 和 iosql 使用指南说明 Charts 全量流式行为、空 symbol 分线和 null 断点。
6. 修复流式请求取消后的 SQLite 清理线程问题：有序读取使用只读连接并允许串行读取器跨工作线程恢复/关闭，适配 ASGI 在线程池中推进响应、在取消线程中清理的生命周期。

## 真实数据浏览器验收

使用只读服务打开 `/home/lsl/Z1/findata/crypto/binance/future_usdt/kline.factor.iosql`，浏览器为 Chromium，页面为 `kline_factor_1m` 的 Charts。

- 源表共 4,183,200 行、29 列、8 个 symbol；时间从 `2022-01-01 00:00 UTC` 到 `2022-12-31 23:59 UTC`。
- 原实现默认把数据压成 2,000 个桶均值；未筛选 symbol 时还将不同币种混合，并用桶中点作为 x。修复后完整范围返回 4,183,200 个原始点和 8 条独立序列，各币行数与源表一致：ADA/BNB/BTC/DOGE/ETH 各 525,600；LTC/SOL/TRX 各 518,400。
- 全范围请求约 34.5 秒、约 126 MB。浏览器渲染期间内存约 56 MB。没有因性能压力减少点数。
- 单独绘制 BTCUSDT RSI 全年返回 525,600 点，约 4.18 秒、约 18 MB。源值范围约 3.09–97.28，平均约 49.96；纵轴范围与 RSI 0–100 语义相符，曲线在细节窗口内显示连续分钟变化。
- 将 BTC RSI 拖拽缩放到约 4.8 天返回 205,147 个原始点，约 1.26 秒；进一步缩到约 5 天中的局部窗口返回 6,903 点，约 40 毫秒；双击复位重新取得 525,600 点。
- 最新前端再验收 1 天、空 symbol、RSI 查询：返回 11,520 点，8 条序列，每个 symbol 1,440 点；浏览器控制台 0 errors / 0 warnings。
- 制造全库 RSI 流加载中切换到 BTC 单日窗口的取消场景。浏览器过滤结果为 1,440 点；服务端随后正常关闭，日志无 `sqlite3.ProgrammingError` 或未释放连接异常。

## 检查

- `python -m py_compile iosql/web/app.py tests/test_chart_pushdown.py`：通过。
- `node --check iosql/web/static/js/tab-charts.js`、`app.js`、`tab-maintain.js`：通过。
- `python -m pytest -q tests/test_chart_pushdown.py`：25 passed（0.90 秒），新增跨线程关闭 SQLite 有序流回归用例。
- `git diff --check`：通过。
- 未运行全仓测试套件。

## 复查与边界

- 后端排序与每个 symbol 的 x 单调性检查避免记录乱序；总行数和分 symbol 数量不匹配时前端拒绝将部分响应标成成功。
- 多币种 SMA/EMA/ATR 等不同价格量纲的序列共用 Y 轴，BTC 价格会压缩低价币曲线。这是尺度显示问题，不是指标值被改写；界面已提示按单一 symbol 查看，或并列比较 RSI 等归一化指标。
- 当前 candle 选项明确是 OHLC 四线，没有蜡烛实体；如产品需要传统红绿蜡烛形态，应另做绘制器设计。
- 完整年度多 symbol 请求约 34.5 秒且传输约 126 MB，这是“不省略选中范围内任何点”的直接成本。用户可缩小时间范围或选择少数 symbol 改善等待时间，仍不会对窗口内数据抽样。
