# PLAN-070：chart 大表曲线绘制失败修复（分桶聚合下推 SQL）

日期：2026-09-28
状态：已实施（结果见 PLAN-070-chart-bigtable-pushdown-OUTCOME.md）

## 1. 问题（用户报告：chart 绘制曲线失败）

Web Charts Tab 对大表（如 binance kline.factor 库 `kline_factor_1m`，418 万行 / 13 个月分区）
自动绘制曲线必然失败，服务端返回 422：

```
{"detail":"扫描行数超过上限 500000","hint":"请缩小时间窗（start/end）或增大分区粒度后再绘图"}
```

根因（`iosql/web/app.py` `_chart_endpoint`）：

1. line 路径采用 Python 流式两遍扫描（第一遍求 x 范围，第二遍逐行分桶平均），
   为防内存/耗时失控设了 `MAX_CHART_SCAN_ROWS = 500_000` 硬上限；
   无时间筛选时 418 万行必然超限 → 直接 422，前端 Charts Tab 进入即自动绘制 → 必失败。
2. 即便取消上限，Python 逐行聚合也太慢：实测全表 2 列流式扫描 5.83s
   （现实现第二遍还 `select *` 24 列，单月窗口就要 3.4s）。
3. candle 路径第二遍扫描 `select *`，存在同样的列投影浪费（前端 candle 实际走
   4 次 line 请求，服务端 candle 分支仅 API 用户使用）。

实测对比（同一大表、全范围、无筛选）：

| 方案 | 耗时 | 结果 |
|------|------|------|
| Python 流式扫描（2 列投影，理想化） | 5.83s | 还须再乘聚合开销 |
| SQL `GROUP BY` 分桶下推（13 分区） | 1.09s | 精确聚合，≤n_buckets 行返回 |
| SQL MIN/MAX/COUNT 探测（13 分区） | 0.36s | — |

## 2. 方案

新增双引擎读方法 `aggregate_by_bucket(x_col, value_cols, n_buckets, where=None)`
（契约冻结规则允许"以新方法叠加"新增能力；本方法为新增读能力，不改既有签名），
把分桶聚合下推到 SQL；web chart 的 line 路径改走该方法，删除 500k 上限。

### 2.1 引擎方法语义（sqlite / postgresql 一致）

```python
def aggregate_by_bucket(self, x_col, value_cols, n_buckets, where=None) -> dict:
    """SQL 端分桶聚合。

    桶网格：idx = floor((x - x_min) / width)，width = span // n_buckets + 1；
    返回（长度均为 n_buckets，空桶 0）：
    {
        "scanned": int,                # x 非空行总数
        "x_min": Any, "x_max": Any,    # 全局 min/max 原始值（text 模式为字符串）
        "x_numeric_rows": int,         # 数值类型 x 行数（PG 强类型=非空行数）
        "x_digit_text_rows": int,      # 纯数字文本 x 行数（YYYYMMDD 等）
        "value_bad_rows": {col: int},  # 数值列非空但非数值的行数（PG 恒 0）
        "sums": {col: list[float]},    # 桶内求和
        "counts": {col: list[int]},    # 桶内非空计数
    }
    """
```

- where 语义与 `select()` 完全一致（dict/list/Case/文本），并保留分区裁剪；
- x 列模式由数据探测分类（sqlite 无约束类型）：
  - 全数值 → numeric 模式；
  - 全纯数字文本 → text 模式（CAST 后按整数分桶，兼容 calendar YYYYMMDD）；
  - 数值与纯数字文本混存 / 含其他文本 → 调用方依据计数给出与现状一致的 422；
- 桶表达式：`CAST((x - :xmin) / :width AS INTEGER)`（float 除法后截断，
  非负数与 Python `//` 结果一致）；text 模式内层再 `CAST(x AS INTEGER)`；
- PG 强类型列：numeric 列 `x_numeric_rows = scanned`；text 列用
  `TRIM(x) ~ '^[0-9]+$'` 探测纯数字文本；numeric 列不可能存文本 → `value_bad_rows` 恒 0。

### 2.2 web chart 改造（`_chart_endpoint`）

- line 路径：调用 `aggregate_by_bucket(x_col, [col], n_buckets, where=comps)`，
  在 web 层完成 x 模式分类（沿用现有 422 文案与 hint：混合类型 / 非数字文本 /
  非数值数据），再按 `mids = x_num_min + i*width + width//2`、`y = sum/count`
  组装与现状完全相同的响应 `{kind:'line', x, y, col, symbol}`；
- 删除 line 路径的两遍流式扫描与 `MAX_CHART_SCAN_ROWS` 422（大表不再失败）；
- candle 路径：保留流式实现与 500k 上限（API 专用；前端 candle 走 4 次 line
  请求自动受益于下推），但第二遍扫描由 `select *` 改为只投影
  `[x_col, open, high, low, close]` 五列，消除宽表扫描浪费；
- 前端（tab-charts.js）无需改动：请求与响应契约不变。

### 2.3 不做的事

- 不改 `/chart` 的 URL/请求/响应契约；
- 不给 candle 路径做下推（open/close 语义需要首末行，复杂度高收益小）；
- 不动 `MAX_CHART_BUCKETS = 4000`、`DEFAULT_CHART_BUCKETS = 600`；
- 不把桶聚合做成公开文档化 API（暂不进 skill references；后续有真实需求再转正）。

## 3. 改动清单

| 文件 | 改动 |
|------|------|
| `iosql/engines/sqlite/query_util.py` | 新增 `build_bucket_stats_query` / `build_bucket_agg_query`（内部固定字段查询，复用 where 构建器，不入用户 fields 白名单） |
| `iosql/engines/sqlite/table.py` | 新增 `aggregate_by_bucket`：两遍分区循环（探测→聚合），复用 `_get_select_part_refs` 分区裁剪 |
| `iosql/engines/postgresql/table.py` | 新增 `aggregate_by_bucket`：单逻辑表两条 SQL（探测+聚合），`_where_sql` 复用 |
| `iosql/web/app.py` | `_chart_endpoint` line 路径接下推；candle 路径列投影 |
| `tests/test_chart_pushdown.py` | 新增：引擎级正确性（数值/text/混合/过滤/空表/浮点 x）+ web 级回归（大表无筛选、calendar 表 ISO 文本 422、响应契约不变） |
| `docs/dev/PLAN-070-…` / `PLAN-070-…-OUTCOME.md` | 计划与结果 |

## 4. 测试计划

1. 引擎级（sqlite）：duration 多分区表上对比下推结果与 Python 逐步计算期望值；
   symbol where 过滤；YYYYMMDD 文本 x；混合类型；非数字文本；浮点 x；空表。
2. PG 级：既有 PG 测试门控模式（无 DSN 自动 skip），覆盖数值 + 文本 x + 过滤。
3. web 级：`/chart` 无 start/end 大表不 422（行数超旧上限的库）；ticks 表响应
   契约不变；events（ISO 日期文本 x）维持 422 + hint。
4. 回归：现有 test_web / test_plan062_web / partition 契约测试全绿。
5. 真机验证：对 8692 端口同一 418 万行大表全范围绘图 <5s 且曲线点数正确。

## 5. 验收标准

- Charts Tab 打开 418 万行 kline 表（无筛选）能直接出图，全程 <5s；
- `/chart?kind=line` 不再出现"扫描行数超过上限"422；
- 既有全部测试与新增测试通过。
