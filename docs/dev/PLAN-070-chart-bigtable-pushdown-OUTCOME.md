# PLAN-070 结果：chart 大表曲线绘制失败修复（分桶聚合下推 SQL）

日期：2026-09-28
计划：PLAN-070-chart-bigtable-pushdown.md

## 结论

用户报告的"chart 绘制曲线失败"已修复：418 万行大表（binance kline.factor
`kline_factor_1m`）打开 Charts Tab 无筛选直接出图，HTTP 200、2000 点，热缓存
约 1.9s（旧路径同场景必 422 失败）；单月窗口 0.83s（旧路径 3.4s，约 4 倍提速）。

## 根因

`_chart_endpoint` 的 line 路径为 Python 流式两遍扫描（第一遍 x 范围、第二遍
逐行分桶），受 `MAX_CHART_SCAN_ROWS = 500_000` 保护；大表无筛选必然超限
返回 422"扫描行数超过上限 500000"，前端 Charts Tab 进入即自动绘制 → 必失败。
且第二遍扫描 `select *`（24 列宽表），即使不超限也慢（单月 3.4s）。

## 变更清单（逐条）

1. `iosql/engines/sqlite/query_util.py`
   - 新增 `build_bucket_stats_query`：探测查询（COUNT/MIN/MAX + typeof 数值
     行数 + TRIM/GLOB 纯数字文本行数 + 各数值列脏值计数）；
   - 新增 `build_bucket_agg_query`：`GROUP BY CAST((x - ?) / ? AS INTEGER)`
     分桶聚合，text 模式内层 `CAST(x AS INTEGER)`；桶参数在前、where 参数在后；
   - 新增 `_bucket_require_column` 列存在性校验（Unknown field 语义）；
   - 均为内部固定字段查询，与 `build_select_count_query` 同信任级别，
     不进用户 fields 白名单路径。

2. `iosql/engines/sqlite/table.py`
   - 新增读方法 `aggregate_by_bucket(x_col, value_cols, n_buckets, where=None)`：
     两遍分区循环（探测→聚合），复用 `_get_select_part_refs` 三态分区裁剪
     （无条件/单 AND/多 OR，与 `_select_queries_for_where` 同构）；
   - x 列模式探测分类：numeric / text（全纯数字文本）/ mixed / non_numeric /
     empty；min/max 仅在纯同类型分区之间合并，规避跨存储类比较；
   - 旧 segment 缺列（schema-on-write 演进）降级：探测退化为仅 x 列、聚合
     跳过该 segment，与旧 `select(*) + row.get(col)→None` 容错等价；
   - 返回 `{scanned, x_min, x_max, x_numeric_rows, x_digit_text_rows,
     value_bad_rows, bucket_min, bucket_width, x_mode, sums, counts}`。

3. `iosql/engines/postgresql/table.py`
   - 同名方法 `aggregate_by_bucket`：单逻辑表两条 SQL（强类型无需 typeof，
     text 列用 `TRIM(x::text) ~ '^[0-9]+$'` 探测纯数字行），桶表达式
     `FLOOR((x - %s) / %s)`（text 模式 `CAST(x AS bigint)`）；
   - 契约声明类型（text/str/string）与 information_schema 类型
     （text/character varying）均识别为文本列；文本数值列直接 BadArgument。

4. `iosql/web/app.py`
   - line 路径改走 `aggregate_by_bucket` 下推，删除 Python 流式扫描与
     500k 上限（`_chart_line_result`）：x 模式 422 文案与 hint 逐字保留
     （混合类型/非数字文本/含非数值数据），响应契约
     `{kind,x,y,col,symbol}` 不变；
   - candle 保留流式实现（API 专用；前端 candle 走 4 次 line 请求自动受益
     下推），扫描上限保留，但第二遍由 `select *` 改为投影
     `[x_col, open, high, low, close]` 五列。

5. 测试
   - 新增 `tests/test_chart_pushdown.py`（16 例）：引擎级与 Python 逐步计算
     对拍（全量/where 过滤/between、text、ISO、浮点 x、脏数据计数、空表）、
     web 级契约回归（无筛选出图、line 不受上限约束、数值逐桶对拍、candle、
     文本 x/y 422、PLAN-055 既有契约）；
   - 新增 `tests/engines/postgresql/test_pg_aggregate_by_bucket.py`（7 例，
     假连接无 live PG）：SQL 形态、参数顺序、模式分类、空表、where 参数位置。

## 验证

- 全量回归：tests/ 主套件 580 passed / 2 skipped；tests/engines 137 passed /
  14 skipped（live PG 无 DSN 自动跳过）；后续复审改动后相关套件
  231 passed 全绿；
- 真机验证（kline.factor.iosql，4,183,200 行 / 13 分区 / 24 列）：
  - 无筛选全范围 `col=SMA(20)[close] buckets=2000` → 200，2000 点，热 1.94s；
  - 单月窗口 500 点 → 200，0.83s（旧路径 3.4s）；
  - symbol 筛选 → 200，1000 点。

## 行为差异说明（相对旧实现）

1. line 大表不再 422（本次修复目标）；
2. 旧路径对"x 列全文本但仅首尾值为纯数字、中间混有脏文本"的表会在分桶期
   `int()` 抛未处理异常（500）；新路径探测计数判定为 non_numeric，统一 422
   并给 hint——由隐性 500 变为显性 422；
3. candle API 路径语义与上限不变，仅减少扫描列。

## 后续建议（未列入本计划）

- candle 分支如需大表全范围，可基于窗口函数做 open/close 下推；
- `aggregate_by_bucket` 目前为内部加速路径未写入对外文档，若库用户需要
  下采样聚合，可按冻结契约"新方法叠加"规则转正并补 references。
