# iosql CLI

`migrate` 支持 SQLite/Parquet 源库，并可写入 SQLite、Parquet 或 PostgreSQL 目标库。
Parquet URI 必须显式传 `parquet://`；DuckDB 随 iosql 默认依赖安装。
其他 CLI 运维命令仍只支持 SQLite。

> 版本对齐：iosql v0.3.x。可视化管理台见 [ui.md](ui.md)。

CLI 入口：

```bash
iosql --help
python -m cli --help  # 源码树内兼容入口，真实实现位于 iosql.cli
```

常规 CLI 运维命令的 `--source` 只支持 SQLite 目录库，可传本地路径或 `sqlite://...` / `sqlite3://...`。`migrate` 是例外：它接受 SQLite 路径/URI 或 `parquet://...` 源库，并接受 SQLite、Parquet 或 PostgreSQL 目标。

## 命令速查表

| 类别 | 命令 | 说明 |
|------|------|------|
| **发现** | `iosql tables -s DB` | 列出逻辑表 |
| | `iosql web -s DB [--write] [--host] [--allowed-host] [--port] [--no-browser]` | 启动 Web 管理台；绑定 `0.0.0.0` 时必须指定具体 `--allowed-host`，其余 Host 头拒绝（默认只读，详见 [ui.md](ui.md)） |
| | `iosql rename-database -s DB --target NEW_DB` | 重命名 SQLite 目录库；覆盖目标需 `--overwrite --yes`，目标须为 iosql 库且非根/家目录/符号链接，失败可回滚 |
| | `iosql rename-table -s DB -t OLD --new-table NEW` | 重命名逻辑表；覆盖目标需 `--overwrite --yes` |
| | `iosql spec -s DB -t TABLE` | 输出表契约 JSON |
| | `iosql layout -s DB -t TABLE` | 输出 `layout_report()` |
| | `iosql parts -s DB -t TABLE` | 输出逻辑分区 |
| | `iosql segments -s DB -t TABLE` | 输出物理 segment |
| | `iosql validate -s DB -t TABLE --deep` | 深度校验 manifest、SQLite 文件和 stats |
| **查询** | `iosql explain -s DB -t TABLE --where JSON` | 输出 `explain_select()` 查询计划 |
| | `iosql query -s DB -t TABLE --where JSON --format jsonl` | 查询小规模结果 |
| **数据进出** | `iosql import -s DB -t TABLE -f rows.jsonl --mode append --batch-size 50000` | 批处理读取并写入既有契约表 |
| | `iosql export -s DB -t TABLE -o rows.jsonl --where JSON --batch-size 50000` | 批处理导出查询结果 |
| **诊断维护** | `iosql doctor -s DB [-t TABLE] [--fix]` | 检查布局和主键索引；`--fix` 只修索引；重分区建议会优先推荐业务时间列 |
| | `iosql compact -s DB -t TABLE [--partition PART]` | 整理 row segment，生成新 generation；`--partition` 会校验分区存在并列可用分区 |
| | `iosql cleanup-obsolete -s DB -t TABLE [--yes]` | dry-run 或确认清理 obsolete segment 文件；`--partition` 同样校验（含仅存在于 obsolete 的分区） |
| | `iosql migrate -s DB --target parquet://... [--dry-run] [--verify row-count]` | SQLite/Parquet 源库迁移到 SQLite、Parquet 或 PostgreSQL；Parquet 源和目标使用显式 URI；拒绝同库/嵌套目标；行数校验要求目标库不存在同名表 |
| | `iosql repartition -s DB -t TABLE --partition 'duration(ts,every=1hour)' --target TARGET` | 按新分区策略写入目标库；`--dry-run` 也校验 interval/timezone 与目标父目录，目标不能等于/嵌套源库 |
| | `iosql backup -s DB --target BACKUP` | 目录级备份；拒绝与源库互相包含的路径 |
| | `iosql restore --backup BACKUP --target DB` | 目录级恢复；备份和覆盖目标均须有真实表级 manifest 且分区文件完整，空目录不能作为备份；覆盖需 `--replace --yes`；复制后的副本会再次校验，替换失败回滚 |
| | `iosql index list/check/create/drop ...` | 检查、创建或删除物理分区索引；`index create` 校验列存在，`index check` 未通过退出码 2 |

## CLI 安全规则

- `repartition` 默认必须提供 `--target` 或 `--replace-source`，不会隐式替换源库。`--dry-run` 也会先解析 interval/timezone 并校验目标父目录，且拒绝目标等于源库/位于源库内部。
- 需要替换源库时必须使用 `--replace-source --yes`；`--dry-run` 不写文件，所以不要求 `--yes`。替换过程原子化：备份用 `os.replace` 改名（近零成本），跨文件系统传 `--backup` 会明确拒绝（EXDEV，源库原封不动），失败自动回滚。
- `restore --replace --yes` 只允许覆盖有真实表级 manifest 和完整分区文件的 iosql 库；备份也须通过同样的校验，空库没有可靠身份标记，当前不支持从空目录恢复。伪造 meta 的普通目录、根/家目录、符号链接以及与备份互相包含的路径全部拒绝。替换前校验临时副本，失败保留原目标。
- `rename-database --overwrite --yes` 同样校验目标身份与特殊路径；`migrate` 拒绝同库/嵌套目标，`--verify row-count` 要求目标库不存在同名表（否则迁移前直接报错）。
- `cleanup-obsolete` 默认 dry-run；只有传 `--yes` 才删除文件，删除前默认执行当前可见 segment 深度校验；`--partition` 拼写错误会报错并列出可用分区（compact 同理）。
- `import` 使用 `table.write()`，目标表必须已有 `TableSpec/TableContract`；JSONL/CSV 按 `--batch-size` 流式读取并支持 UTF-8 BOM，坏 JSONL 行/CSV 字段数不匹配会报行号；中途失败输出 `rows_seen`/`rows_written`/`batches_committed` JSON 汇总，以退出码 1 结束。
- `query` 适合排查和小规模输出到终端；`export` 使用 `--batch-size` 批处理写文件，适合无全局排序的大结果集导出；`export` 先写临时文件再原子替换输出，失败保留旧文件。JSON 中 bytes 列统一 base64。
- 复杂 where 优先传 JSON 或 `@file`，例如 `--where '{"field":"symbol","op":"==","value":"AAPL"}'`。
- 所有命令失败统一输出红色单行错误（不裸 traceback）；退出码：`0` 成功、`1` 业务失败、`2` 校验未通过（validate/index check/doctor）、`130` 用户中断。
