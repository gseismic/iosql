# PLAN-034-cli-operational-commands-OUTCOME

## 结果

本轮已按 `PLAN-034` 完成 CLI 运维入口扩展，并修复现有 `repartition` 的正确性和安全性问题。

设计文档：

- `docs/design/cli-20260621-operational-design.md`

计划文档：

- `docs/dev/PLAN-034-cli-operational-commands.md`

## 主要变更

### 1. CLI 入口迁移

将真实 CLI 实现迁移到：

- `iosql/cli.py`

原因是顶层 `cli` 包名过于通用，测试运行时曾导入到其他项目的 `cli.__main__`。现在：

- `pyproject.toml` 的 console script 改为 `iosql = "iosql.cli:app"`。
- `pyproject.toml` 只打包 `iosql*`，避免继续发布通用顶层 `cli` 包。
- `cli/__main__.py` 保留为源码树内兼容包装，只负责 `from iosql.cli import app`。

### 2. 新增只读检查命令

新增：

- `iosql tables`
- `iosql spec`
- `iosql layout`
- `iosql parts`
- `iosql segments`
- `iosql explain`
- `iosql query`

这些命令覆盖表发现、表契约、布局诊断、逻辑分区、物理 segment、查询计划和小规模查询。

### 3. 新增数据进出命令

新增：

- `iosql import`
- `iosql export`

支持格式：

- `json`
- `jsonl`
- `csv`
- `auto`，按扩展名识别。

导入使用 `table.write()`，目标表必须已有契约。

### 4. 修复并硬化 doctor

变更：

- `doctor` 改为遍历所有物理 SQLite 文件检查主键列索引。
- `--fix` 通过 `table.create_index()` 为所有物理 refs 创建缺失索引。
- 布局类问题无法自动修复时，即使传了 `--fix` 也保留非零退出码。
- 分区周期建议改成核心可解析的表达，如 `1hour`、`4hour`、`1day`、`1week`、`2week`、`3month`、`2year`。

### 5. 修复并硬化 repartition

变更：

- `--every` 现在通过 `old_spec.with_partition(...)` 真正写入新 `TableSpec`。
- 默认不再覆盖源库；必须提供 `--target`。
- 如需替换源库，必须同时提供 `--replace-source --yes`。
- 增加 `--dry-run`、`--target-table`、`--batch-size`、`--kind`、`--timezone`。
- `--replace-source --dry-run` 不要求 `--yes`，因为 dry-run 不写文件。
- 目标目录已存在时默认失败。

### 6. 新增备份恢复与索引命令

新增：

- `iosql backup`
- `iosql restore`
- `iosql index list`
- `iosql index check`
- `iosql index create`

`restore` 覆盖已有目标时必须同时提供 `--replace --yes`。

### 7. 新增 CLI 测试

新增：

- `tests/test_cli.py`

覆盖：

- help 命令展示新增命令。
- 非 SQLite URI 拒绝。
- `tables/spec/layout/parts/segments`。
- `explain/query/export/import`。
- `doctor --fix` 和 `index check/list`。
- `backup/restore`。
- `repartition` 的安全失败、dry-run 和目标库生成。

## 验证

已运行：

```bash
python -m py_compile iosql/cli.py cli/__main__.py
python -m cli --help
pytest tests/test_cli.py
pytest tests/engines/sqlite/test_timeseries_contract.py::test_repartition_to_writes_new_database_without_touching_source
python -m pip wheel . --no-deps -w /tmp/iosql-wheel-review
git diff --check
```

结果：

- `tests/test_cli.py`：8 passed。
- `test_repartition_to_writes_new_database_without_touching_source`：1 passed。
- wheel 验证：包含 `iosql/cli.py`，不包含顶层 `cli/` 包，console script 为 `iosql = iosql.cli:app`。
- `git diff --check`：无空白错误。

## 遗留项

本轮未实现以下设计中的后续扩展：

1. `iosql create-table --spec spec.json`。
2. `iosql latest/asof` 专用入口。
3. `iosql validate --deep` 深度校验 manifest、物理文件、row_count 和 SQLite integrity。
4. `iosql compact` 或 segment vacuum。
5. PostgreSQL CLI。

这些不属于本轮 `PLAN-034` 的实施范围。
