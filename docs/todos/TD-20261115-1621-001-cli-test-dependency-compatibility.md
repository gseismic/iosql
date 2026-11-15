# TD-20261115-1621-001：评估较新 Typer/Click 下的 CLI 测试兼容性

- 创建时间：2026-11-15 16:21（Asia/Shanghai）。
- 模型：Codex（环境说明基于 GPT-6，未提供可核实的实际模型 ID）。
- 初始状态：待评估。
- 来源：用户要求 PostgreSQL 引擎 review-fix-loop，扩大回归时发现的范围外测试兼容性问题。
- 建立版本：`620ee5e040159cb458c2fb60068e6352ee07b355`。在该完整 HEAD 的独立副本复现；本轮 PostgreSQL 修复仍为 WORKTREE，不是该问题的引入依据。

## 2026-11-15 16:21 提出

模型同上，状态为“待评估”。Python 3.11.15、Typer 0.19.2、Click 8.3.0 下，未修改 HEAD 的以下测试得到 3 failed、1 passed：`tests/test_cli.py:514`、`tests/test_cli.py:547` 从 `result.output` 读取 JSON，混入迁移标准错误进度后解码失败；`tests/test_migrate_progress.py:158` 假定 `typer.core.rich` 属性存在，当前版本没有该属性。这是测试与依赖版本的兼容性证据，不据此声称 CLI 实际标准输出或迁移业务行为错误。

保持 Arrow 1.3.0、DuckDB 1.5.4、pytest 8.4.2 等核心依赖，并使用项目依赖范围内的 Typer 0.12.5、Click 8.1.8，HEAD 上对应四个参数化用例为 4 passed。项目目前未固定测试依赖组合。

目标：明确 CLI 回归所支持的 Typer/Click 范围，按标准输出/标准错误通道验证结果，避免假定可选富文本依赖的内部属性固定不变。价值：让依赖升级后的回归结果能够区分真实行为错误与测试桩、捕获方式变化。

范围：CLI 测试及其依赖矩阵/运行说明；不改变 PostgreSQL 事务、查询、迁移业务语义。本次只记录，不实施范围外 CLI 修改。

完成标准：在明确的基线版本和较新受支持版本上执行对应测试，确认 JSON 仅从标准输出解析、进度在标准错误验证，富文本和普通帮助两个场景均通过；若决定收窄依赖范围，应单独说明兼容性依据。优先级尚未确定，未视为已承诺开发任务。

关联计划：`docs/plans/PLAN-20261115-005-model-unknown-postgresql-correctness.md`。
