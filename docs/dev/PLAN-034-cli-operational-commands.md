# PLAN-034-cli-operational-commands

## 目标

把现有 CLI 从临时 `doctor/repartition` 工具扩展为可用于日常 iosql SQLite 目录库运维的入口，并先修复 `repartition` 的正确性和安全性问题。

详细设计见：

- `docs/design/cli-20260621-operational-design.md`

## 范围

本轮实施：

1. 修复 URI 解析，只支持 SQLite URI 或本地路径，拒绝其他 URI。
2. 增加只读检查命令：`tables`、`spec`、`layout`、`parts`、`segments`、`explain`、`query`。
3. 增加数据进出命令：`import`、`export`。
4. 修复并硬化 `doctor`：
   - 遍历所有物理 refs 检查主键列索引。
   - 使用核心可解析的分区周期建议。
5. 修复并硬化 `repartition`：
   - `--every` 真正写入目标 `TableSpec`。
   - 默认写入 `--target`，源库替换必须 `--replace-source --yes`。
   - 支持 `--dry-run`、`--target-table` 和 `--batch-size`。
6. 增加 `backup`、`restore`。
7. 增加 `index list/check/create`。
8. 增加 CLI 测试覆盖主要工作流。

本轮不实施：

1. `create-table`。
2. `latest/asof` 专用 CLI。
3. 深度 manifest 校验。
4. segment compaction/vacuum。
5. PostgreSQL CLI。

## 实施步骤

1. 将真实 CLI 实现迁移到 `iosql/cli.py`，保留源码树内 `cli/__main__.py` 兼容包装，并只打包 `iosql*`：
   - 增加 JSON/CSV/JSONL 读写 helper。
   - 增加 SQLite URI/path 校验 helper。
   - 增加物理分区文件和索引枚举 helper。
2. 添加只读命令：
   - `tables`
   - `spec`
   - `layout`
   - `parts`
   - `segments`
   - `explain`
   - `query`
3. 添加数据进出命令：
   - `import`
   - `export`
4. 修复 `doctor`：
   - 替换 `_missing_indexes()` 的单文件检查逻辑。
   - `--fix` 后重新检查并输出修复结果。
5. 修复 `repartition`：
   - 用 `TableSpec.with_partition()` 派生新 spec。
   - 支持 dry-run 和安全替换。
6. 添加 `backup`、`restore` 与 `index` 子命令。
7. 新增 `tests/test_cli.py`。
8. 运行验证：
   - `python -m cli --help`
   - `python -m cli repartition --help`
   - `pytest tests/test_cli.py`
   - 视修改影响运行相关 sqlite 契约测试。

## 验收标准

1. `python -m cli --help` 展示新增命令。
2. 样例 iosql 库可通过 CLI 完成：
   - 列表、契约、布局、分区、segment 查看。
   - explain/query/export。
   - import JSONL。
   - backup/restore。
3. `repartition --target --every 1hour` 生成目标库，目标表契约分区周期变化。
4. 不带 `--target` 且没有 `--replace-source --yes` 的 `repartition` 失败，不修改源库。
5. `doctor --fix` 可为所有物理 refs 创建缺失主键索引。
6. CLI 测试通过。

## 风险与控制

1. 风险：CLI 直接操作目录库，覆盖目标可能造成数据损失。
   - 控制：默认拒绝覆盖；源库替换必须双确认。
2. 风险：CSV 导入缺少类型信息。
   - 控制：CSV 保持字符串，复杂类型使用 JSON/JSONL。
3. 风险：where 字符串和 JSON where 混用可能导致用户误解。
   - 控制：文档和 help 中优先推荐 JSON where；非 JSON 字符串按核心 where 文本处理。
4. 风险：大型 query/export 一次性读入内存。
   - 控制：本轮定位为排查和小规模导出；后续再做流式导出。
