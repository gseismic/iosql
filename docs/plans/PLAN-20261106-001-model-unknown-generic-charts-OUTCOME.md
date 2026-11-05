# Web 通用图表与 mchart3 接入结果

- 完成时间：2026-11-06 01:34（Asia/Shanghai）。
- 执行模型：环境说明为 GPT-6，完整模型 ID 未提供，使用 `model-unknown`。
- 代码范围：`WORKTREE`，基准提交 `1050d43d78d27ae5af46a26cf2ea3388614df2d1`；图表相关代码、测试和本文尚未固定为提交。工作区同时存在其他任务的未提交文件，本结果不将其计入本次实现。
- 计划：`docs/plans/PLAN-20261106-001-model-unknown-generic-charts.md`。
- 设计：`docs/design/chart-20261106-generic-axes.md`。

## 完成情况

- Charts 改为先设置通用筛选条件、X/Y 字段和可选分组，再主动绘制；依据字段类型选择折线或散点，并保留 OHLC 四线。进入页面不会自动扫描全表。
- 新增只读 `POST /api/table/{name}/chart`，复用条件解析、列投影和流式读取；保留原有 GET 图表接口。原始数据不抽样、不聚合，空 Y 值保留为断点，超出分组或类别上限时明确报错。
- 使用随安装包分发的 mchart3 0.5.0，在首次绘制时加载；加载失败可重试，过期请求不会覆盖当前图表。数值和毫秒时间轴按可视范围重新读取，日历日期和类别轴在已读取数据中缩放。
- 针对 PostgreSQL 重开普通表后契约可能为空的情况，回退到物理列类型，不虚构时间或分组元数据。补充日历日期、空值、精度和大整数处理。
- 更新 README、接口及浏览器回归测试，保留图表资源的来源、许可证和校验信息。

## 验证结果

- 运行 `python -m pytest tests/test_chart_pushdown.py tests/test_web_browser.py tests/test_web.py tests/test_plan062_web.py tests/test_plan072_fixes.py tests/test_distribution.py -q`：192 项通过，6 条依赖弃用警告。覆盖 SQLite、Parquet、独立 PostgreSQL 临时 schema、旧接口、条件、类型、边界、图表加载与请求竞态，并验证分发包资源。
- 对用户指定的只读原库启动独立测试服务：筛选 `symbol = 0GUSDT` 后绘制 1441 个点，与普通查询的时间和值逐点一致；不分组、以 `symbol` 为类别轴的散点测试返回 1608 条原始记录。浏览器未报告错误，mchart3 按需加载；独立测试服务已停止。
- `node --check`、`python -m py_compile iosql/web/app.py` 和 `git diff --check` 通过。

## 计划偏差与遗留事项

- 根据实施中发现的实际情况，按计划补充 PostgreSQL 物理类型回退、日历日期支持与图表引擎按需加载；均已在计划中记录。
- 单次最多显示 32 组、每个类别轴最多 1000 类；超限需缩小筛选范围。大范围原始数据仍可能消耗较多查询时间和浏览器内存，建议先添加筛选条件。上述边界和缩放行为已写入 README。
- 原库小窗口验证不能代表全表性能。尚未提交或推送。
