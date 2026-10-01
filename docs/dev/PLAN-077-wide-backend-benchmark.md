# PLAN-077：SQLite 与 Parquet 200 列宽表性能基准

## 背景

iosql 已同时支持 SQLite 与 Parquet 目录后端，但当前 benchmarks 主要覆盖 SQLite 写入调优，缺少同机、同 schema 下的宽表后端对比。宽表会放大行式与列式存储在写入、全量读取和列投影上的差异。

## 目标

在 `benchmarks/` 增加可重复执行的 SQLite/Parquet 宽表对比工具，并在当前环境以多个数据规模实测。表固定为 200 列（1 个整数主键、199 个浮点列），结果记录写入吞吐、全列扫描、窄列投影、条件读取和数据占用。

## 范围与口径

1. 两个后端使用相同的 iosql `Database` / `Table` API、表契约、数据生成器和 segment 上限。
2. 记录 `write()` 调用累计耗时与包含数据生成的总写入耗时，避免混淆 API 写入和端到端吞吐。
3. 使用 `Segment.rows(stats_columns=["id"])`，写入批次默认与 segment 上限相同，避免 Parquet 活跃文件被小批次反复重写。
4. 分别测量 `count_rows()`、全部 200 列流式读取、4 列投影和按 id 的选择性读取；记录过滤计划扫描的 segment 数。
5. 每个后端/规模使用新库；默认实测规模为 1,000、10,000、50,000、100,000 行。完成后清理临时数据库，只保留 JSON 和 Markdown 报告。
6. 结果注明机器、软件版本、批次/segment 设置和文件系统，避免把单机结果误认为普适结论。

## 实施步骤

- [x] 1. 新增 `benchmarks/benchmark_wide_backends.py`，支持自定义规模、批次、重复次数、临时根目录和报告路径。
- [x] 2. 生成配套 Markdown 汇总，并对每个规模比较 Parquet 相对 SQLite 的吞吐、读取耗时与数据体积。
- [x] 3. 运行多规模实测，核对 200 列、实际行数、投影/过滤行数和 segment 计划。
- [x] 4. Review 基准实现与结果，记录限制和运行环境到本计划的 OUTCOME 文件。

## 验收标准

- 每种后端都严格使用 200 列，行数和确定性抽样正确。
- 写入、计数、全列扫描、窄投影及选择性读取均生成可比指标。
- 可通过 CLI 自定义行数列表和写入参数；JSON 与 Markdown 可重现完整配置。
- 当前环境至少完成四种数据规模的两后端对比，报告文件保存在 `benchmarks/`。
- `git diff --check` 通过；不修改既有后端实现和用户已有工作区变更。

结果：[PLAN-077-wide-backend-benchmark-OUTCOME.md](PLAN-077-wide-backend-benchmark-OUTCOME.md)。
