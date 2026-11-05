# SQLite 到 Parquet 迁移性能优化结果

- 完成时间：2026-11-05 18:44 Asia/Shanghai
- 执行模型：环境仅提供 GPT-6 系列信息，实际模型 ID 未提供；沿用计划文件名的 `model-unknown`。
- 更新：2026-11-05 18:46 Asia/Shanghai；同一执行模型补充最低依赖版本验证。
- 实施范围：`WORKTREE`，基准提交 `35275e989349ce55a65268f0da6d5fbb5c3300b9`。涉及 `iosql/migration.py`、`iosql/_migration_sqlite_parquet.py`、`iosql/engines/parquet/_bulk.py`、`iosql/engines/parquet/table.py`、`iosql/engines/sqlite/table.py`、`iosql/utils/storage_identity.py`、`iosql/cli.py`、相关测试与使用说明；其中若干文件还有其他任务的未提交改动，完整结果尚未固定为提交。
- 关联计划：`docs/plans/PLAN-20261105-005-model-unknown-sqlite-parquet-migration-performance.md`。

## 完成情况

- SQLite 到 Parquet 的正式迁移按物理文件交错统计和读取；源数据与独立 `COUNT(*)` 在同一 SQLite 语句快照中获取。空文件也返回计数，缺失或读取失败不得发布。源总行数未知期间，CLI 显示已复制行数，最终结果仍包含源、目标实际行数。干运行与其他后端保持原流程，自定义源计数或批读取方法时回退原流程。
- Parquet append 在类型可安全等价时采用列式导入：安装 PyArrow 时使用 Arrow；未安装时使用 DuckDB 列表参数。NaN、非规范类型、其他冲突模式和小批次回退原有逐值路径。保留批内及跨数据片主键约束。
- 在源、目标分区相同且不使用 segment 的情况下，大源分区按边界输出批次；小分区仍可以合批。减少无变化 schema 检查和单分区存在性检查中的全量分区扫描。
- 暂存 Parquet 目标计数在单次遍历中复用 DuckDB 连接，并检查完整数据片清单；两次既有深度结构检查分别复用各自的连接，没有合并或跳过发布前验证。
- 更新 README 和 `skills/iosql-usage/references/cli.md`，解释正式迁移中的未知总数与预览批次数的区别；公共 API 和依赖清单未改变。

## 验证结果

- 在用户命令涉及的 M3 源盘（USB/ext4）和 Z1 目标盘（SATA NTFS/fuseblk）分别建立隔离临时目录，复制两个真实源分区，共 256,320 行、13 列；两次测试使用已预热的文件缓存。禁用本任务的源读取与批量导入快速路径以模拟旧热路径，带 `row-count` 迁移为 19.966 秒；启用优化为 2.459 秒。两次均 `published=true`、`verified=true`，源和目标行数均为 256,320；对两份 Parquet 结果做双向全列 `EXCEPT ALL`，差集为空。文件数由 7 个减少为 6 个。仅单次样本、缓存预热，不代表 9,984 个分区全库的最终耗时；临时副本已清理。
- 在本机临时目录的同样样本中，未安装 PyArrow 的等价回退路径通过禁用可选导入实测为 4.370 秒；输出与旧路径双向全列差集为空。进程中禁止导入 PyArrow 的专项测试也通过。
- 101 个小 Parquet 文件的独立热缓存微基准：逐文件新建连接计数 0.665 秒，复用一个连接计数 0.038 秒；两种计数一致。此结果仅用于验证连接复用的局部开销。
- 独立 PostgreSQL 18 临时实例下运行完整测试：1,362 项通过、19 项跳过；其后新增/调整的性能、进度与 save_as 测试组合为 52 项通过。初轮完整测试暴露自定义批读取覆盖被绕过，已恢复兼容并重新通过。
- 临时隔离安装项目声明的最低版本 DuckDB 1.1.0，运行性能、进度与 Parquet 测试：62 项通过；临时安装目录已清理。
- 覆盖空分区、未合并 WAL、缺失文件、读取丢行、单连接配额、NaN、二进制、NULL、主键冲突、既有目标合并、不同布局、可选依赖缺失和受损目标检查。实际 CLI 小样本输出的 JSON 可解析，`published` 和 `verified` 均为 true。
- `ruff check --select F`（新增模块及相关文件）、Python 3.9 语法解析和 `git diff --check` 通过。

## 计划偏差与遗留状态

- 目标的两次深度检查保留原先独立的调用，只降低每次检查的连接创建成本，避免削弱发布前完整性保障。
- 实际批次数可因分区边界高于干运行估计；`row-count` 检查行数，并不等同于完整内容校验。
- 未执行用户的全库真实迁移，实际全库性能取决于冷缓存、磁盘负载、分区分布和数据类型。未提交、未推送；保留已有工作区改动。
