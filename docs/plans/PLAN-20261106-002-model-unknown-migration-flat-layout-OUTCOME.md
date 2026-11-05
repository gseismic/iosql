# Parquet 迁移目标平坦目录结果

- 完成时间：2026-11-06 01:52 Asia/Shanghai
- 执行模型：Codex，环境仅提供 GPT-6 系列信息，实际模型 ID 未提供；沿用计划中的 `model-unknown`。
- 实施范围：`WORKTREE`，基准提交 `1050d43d78d27ae5af46a26cf2ea3388614df2d1`。本次修改 `iosql/migration.py`、`iosql/coordination.py`、`iosql/_parquet_flat_publish.py`、相关测试及迁移说明；工作区还有其他任务的 Web 和文档改动，结果尚未固定为提交。
- 关联计划：`docs/plans/PLAN-20261106-002-model-unknown-migration-flat-layout.md`。
- 设计：`docs/design/parquet-flat-publish-20261106-overview.md`。

## 完成情况

- Parquet 目标在迁移成功后直接使用 `目标库/表名/`，与普通写入一致。SQLite 目标仍使用原代际发布；PostgreSQL 路径未改变。
- 已有目标先暂存完整视图，在库级独占租约内持久化提交决定；已有表保留 SQLite 元数据文件 inode，通过 SQLite backup 更新元数据，并切换 `data` 目录。未选表留在原位。异常中断由后续 iosql 操作先恢复；非协作文件访问不受该屏障保护。
- 支持默认目标盘工作区、跨文件系统显式工作区和目标根内工作区。正常发布后优先隔离任务暂存，再于锁外清理；根挂载点且跨文件系统时，支持的平台使用固定目录句柄安全清理，不支持的平台保留暂存并提示。失败或异常中断遗留的暂存可能保留供检查。
- 按用户要求不支持直接向旧 Parquet 代目录目标追加迁移；旧代目录仍可作为源读取。指定库已通过一次性脚本完成整理，不需要兼容改造其他库。

## 用户指定库操作

- 目标：`/media/lsl/Z1/finbase.crypto/binance/future_usdt/kline.parquet-6h.iosql`；原代任务 ID 为 `a279f8a9abdc498ea62d789f01d4a72a`。目前根目录直接包含 `kline_1m/`。
- 在同一文件系统下持独占协调锁，将整个表目录移动到库根，移动旧代控制标记并更新控制记录；未复制、重编码或逐个修改业务数据文件。旧代标记及原控制记录保留在目标父目录的 `.iosql-locks/`；临时脚本为 `/tmp/iosql-reorganize-parquet-layout.py`。
- 整理前后 21,906 个数据目录文件的相对路径、设备号、inode、大小和修改时间一致，共 17,342,410,576 字节；三个代表性 Parquet 文件的 SHA-256 一致。逻辑表为 `kline_1m`，9,984 个分区，样本行读取成功。这不是对全部数据文件重新计算内容哈希，也未重新执行全表行数比较。
- 在同一 NTFS/FUSE 目标盘独立临时库先演练目录切换和三个中断位置：中断后阻止正常访问，脚本重跑恢复，样本内容哈希与查询一致。该盘不支持 Linux `RENAME_NOREPLACE`，一次性脚本通过预留空目录和身份核对处理。

## 验证结果

- 隔离 PostgreSQL 18 实例下完整测试：1,448 passed、19 skipped；其后补充的恢复重试、重复迁移、冲突模式及能力检查通过针对性回归。
- 当前代码的迁移、Parquet、保存、安全发布和恢复相关测试：199 passed；其后新增的挂载点清理及不支持安全清理的回退共 3 项专项通过。
- 用户 Z1 的 NTFS/FUSE 盘上运行新布局专项：19 passed。实际 `iosql migrate` 在该盘隔离小库上返回 `published=true`、`verified=true`、源与目标均 2 行，目标根目录直接包含 `kline_1m`。
- `ruff check --select F`、Python 3.9 语法解析和 `git diff --check` 通过。

## 偏差与遗留

- 根据用户当前使用需要，先验证并整理指定库，再完成未来迁移的平坦发布实现；未变更其他业务库或用户已有工作区改动。
- 旧代目录的 Parquet 库不能直接作为新迁移的目标，符合本次不要求兼容旧布局的授权。Windows 原机和断电行为未验证；Python 3.9 仅做语法检查，未在该版本运行测试。
- 未提交、未推送。
