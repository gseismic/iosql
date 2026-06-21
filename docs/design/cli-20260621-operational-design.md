# iosql CLI 运维入口详细设计

## 背景

当前 `iosql` 已有主 API 能覆盖表契约、分区、segment、查询裁剪、布局诊断和重分区；CLI 只暴露了 `doctor` 与 `repartition` 两个命令。它能作为临时运维脚本使用，但还不是完整的数据库管理入口。

现有 CLI 的关键缺口：

1. `repartition --every` 未真正改变目标分区周期，并且用运行时 `Partition` 对象拼接 `TableSpec`，存在运行时错误风险。
2. `repartition` 默认覆盖源库，虽然有备份，但破坏性操作缺少显式确认和 dry-run。
3. `doctor --fix` 只检查第一个物理分区文件，不能证明多分区、多 segment 的索引完整性。
4. 缺少表发现、契约查看、布局查看、segment 查看、查询计划、查询和导入导出等日常命令。
5. 缺少 CLI 级测试，当前只能通过 `--help` 判断入口是否加载成功。

本设计的目标是把 CLI 做成“本地 SQLite iosql 库的低风险运维入口”，而不是复制完整 Python API。

CLI 的正式打包入口是 `iosql.cli:app`。源码树内保留 `python -m cli` 兼容包装，但发布包不再包含通用顶层 `cli` 包，避免与其他项目的 `cli` 包互相覆盖。

## 目标

本轮 CLI 应覆盖三类高价值工作流：

1. 发现与诊断：快速知道库里有哪些表、表契约是什么、分区/segment 布局是否合理、查询会扫描哪些文件。
2. 数据进出：支持 JSON/JSONL/CSV 的简单导入、查询和导出，便于排查与小规模数据迁移。
3. 安全维护：修复重分区命令，让源库替换变成显式行为；补充备份、恢复和索引维护入口。

## 非目标

本轮不实现以下能力：

1. 不设计完整交互式 shell。
2. 不实现 PostgreSQL CLI 管理。当前命令只支持 SQLite 目录库或 SQLite URI。
3. 不实现任意 SQL 执行入口，避免绕过 iosql 的分区和 where AST。
4. 不实现新表契约创建命令。建表仍通过 Python API 明确声明。
5. 不实现 compaction、vacuum segment 或 tombstone；这些属于后续存储层设计。

## URI 与安全规则

CLI 接收两类 `--source`：

1. `sqlite://path/to/db.iosql`
2. `path/to/db.iosql`

如果输入包含 `://` 但不是 `sqlite://` 或 `sqlite3://`，命令直接失败。这样可以避免把 `postgresql://...` 错误包装成 SQLite 路径。

所有会写文件或移动目录的命令遵守以下规则：

1. 目标路径已存在时默认失败，除非命令提供显式覆盖选项。
2. 替换源库必须同时提供 `--replace-source` 与 `--yes`；`--dry-run` 不写文件，因此不要求 `--yes`。
3. 支持 `--dry-run` 的维护命令在 dry-run 下只输出计划，不创建备份、不写目标库。
4. 备份和恢复用目录级 `shutil.copytree`，保持 iosql SQLite 目录库结构。

## 命令设计

### `iosql tables`

列出库内逻辑表。

输入：

- `--source/-s`
- `--json`

输出：

- 默认逐行输出表名。
- `--json` 输出 `{"tables": [...]}`。

### `iosql spec`

输出表契约。

输入：

- `--source/-s`
- `--table/-t`

输出：

- JSON 格式的 `table.spec().to_dict()`。

### `iosql layout`

输出布局诊断报告。

输入：

- `--source/-s`
- `--table/-t`
- `--summary`：只输出摘要。

输出：

- 默认 JSON，字段来自 `LayoutReport.to_dict()`。
- summary 输出行数、分区数、oversized、undersized 和 recommendation。

### `iosql parts`

输出逻辑分区列表。

输入：

- `--source/-s`
- `--table/-t`
- `--json`

输出：

- 默认逐行输出。
- JSON 输出 `{"table": ..., "parts": [...]}`。

### `iosql segments`

输出物理 segment 列表。

输入：

- `--source/-s`
- `--table/-t`

输出：

- JSON 数组，字段来自 `table.segments()`。

### `iosql explain`

输出查询计划，用于验证逻辑分区和 segment 裁剪。

输入：

- `--source/-s`
- `--table/-t`
- `--fields`
- `--where`：JSON where 或 `@file`
- `--order-by`
- `--limit`

输出：

- JSON 格式的 `QueryPlan.to_dict()`。

### `iosql query`

查询小规模结果。

输入：

- `--source/-s`
- `--table/-t`
- `--fields`
- `--where`
- `--order-by`
- `--limit`
- `--format json|jsonl|csv`

输出：

- 默认 JSON 数组。
- JSONL 一行一条。
- CSV 使用结果列名作为表头。

### `iosql export`

把查询结果写入文件。

输入：

- 与 `query` 相同。
- `--output/-o`

输出：

- 写入 JSON、JSONL 或 CSV 文件。
- 命令行输出写入行数和文件路径。

### `iosql import`

把 JSON、JSONL 或 CSV 文件写入既有表。

输入：

- `--source/-s`
- `--table/-t`
- `--file/-f`
- `--format json|jsonl|csv|auto`
- `--mode append|ignore|upsert|replace`
- `--batch-size`

行为：

- 目标表必须已有契约，因为 `table.write()` 需要主键。
- `auto` 根据扩展名识别格式。
- CSV 值保持字符串，复杂类型应使用 JSON/JSONL。
- 按 batch 调用 `table.write()`。

### `iosql doctor`

保留原命令，但修正索引检查策略。

行为：

- 遍历所有物理 refs 检查主键列索引。
- `--fix` 调用 `table.create_index()`，由核心 API 在所有物理 refs 上创建索引。
- 布局建议使用核心可解析的周期表达：`1week`、`2week`、`1month`、`3month`、`2year`。

### `iosql repartition`

重分区到新目标库，默认不覆盖源库。

输入：

- `--source/-s`
- `--table/-t`
- `--every/-e`
- `--kind duration|calendar|range`
- `--timezone`
- `--target`
- `--target-table`
- `--batch-size`
- `--dry-run`
- `--replace-source`
- `--yes`
- `--rm-backup`

行为：

1. 读取旧 `TableSpec`。
2. 使用 `old_spec.with_partition(every=..., kind=..., timezone=...)` 生成新 spec。
3. 如果传 `--target`，只写目标库。
4. 如果不传 `--target`，必须传 `--replace-source`；实际替换还必须传 `--yes`。命令先写临时目标，再备份源库，然后替换源库。
5. dry-run 输出源、目标、新 spec 和预估源布局，不写文件。

### `iosql backup` / `iosql restore`

目录级备份和恢复。

规则：

- `backup` 默认生成 `db.iosql.backup_YYYYMMDD_HHMMSS`。
- `restore` 默认拒绝覆盖已存在目标；覆盖必须传 `--replace --yes`。

### `iosql index`

索引维护子命令。

子命令：

- `index list`：列出每个物理文件上的索引。
- `index check`：检查主键列索引是否齐全。
- `index create`：为表所有物理 refs 创建某列索引。

本轮不实现 `index drop`，因为 drop 对全库性能影响大，且当前主 API 没有表级 drop index 抽象。

## 输出格式

默认规则：

1. 机器可消费命令优先 JSON。
2. 列表类命令默认人类可读，提供 `--json`。
3. 错误输出到 stderr，退出码非 0。
4. 写入类命令输出摘要 JSON 或简短文本，不打印大量行数据。

## 测试设计

新增 `tests/test_cli.py`，使用 `typer.testing.CliRunner`。

覆盖项：

1. `tables/spec/layout/parts/segments` 能读取一个样例库。
2. `explain/query/export` 能按 where 返回正确结果和计划。
3. `import` 能把 JSONL 写入既有表。
4. `doctor --fix` 能补齐手工删除的索引。
5. `repartition --dry-run` 不写目标。
6. `repartition --target` 正确生成新库，并按 `--every` 改变分区周期。
7. `backup/restore` 能复制和恢复目录库。
8. 非 SQLite URI 被拒绝。

## 后续扩展

1. `iosql create-table --spec spec.json`：从 JSON 契约创建空表。
2. `iosql latest/asof`：针对金融截面查询提供专用入口。
3. `iosql validate --deep`：校验 manifest、物理文件、row_count 和 SQLite integrity。
4. `iosql compact`：等待 segment tombstone/compaction 设计落地后再实现。
