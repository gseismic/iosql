# PLAN-084 结果：迁移审查问题修复

- 时间：2026-10-18 15:55（Asia/Shanghai）。
- 模型：Codex（GPT-6，精确模型 ID 未提供）。
- 版本：WORKTREE，基准 `abc30fee0d812626d140bb3d9fcf7a870ac953e7`。
- 计划：[PLAN-084](PLAN-084-model-unknown-migration-review-fixes.md)。
- 设计：[统一迁移设计第 13 节](../design/migration-api-20261018-unified.md)。
- 验证：[验证说明与执行记录](../tests/20261018-15-WORKTREE/migration-review-verification.md)。
- 状态：修复及本机验证完成，未提交或推送，用户 `.zcodeignore` 保留。

## 已完成的八项修复

| 问题 | 实施结果 | 主要位置 |
| --- | --- | --- |
| 发布后清理误删复用路径 | 持独占租约将旧内容移到稳定协调空间中的任务专属退休目录，锁外只删除已拥有的目录；移动失败保留旧内容并报告，不撤销已提交的新视图 | iosql/migration.py |
| 快照漏掉合法表名 | 区分普通 SQLite 侧车文件与目录，保留 -wal/-shm 后缀及 .iosql-locks、.iosql.write.lock 业务表；验证备份、合并和跨文件系统发布 | iosql/migration.py |
| Windows 普通操作失败 | POSIX 保留 flock，缺少 fcntl 时使用 ctypes 惰性绑定的 Win32 共享/独占锁；仅锁冲突进入等待，失败关闭 FD；路径大小写身份与目录同步按平台处理 | iosql/file_lock.py、iosql/coordination.py |
| 合法空目标被拒绝 | allow_empty=True 接纳全部为经过验证 scaffold 的空库，任意目录仍拒绝接管 | iosql/utils/storage_identity.py |
| 空表声明刷新丢失 | Database 保留仍存在的缓存对象；Table 重建后仅在没有持久契约时恢复原内存声明，新发布的持久契约优先 | iosql/database.py、iosql/table.py |
| 内部关闭状态残留 | connect 成功后清除关闭标记，显式 connect 可在关闭后重开；覆盖表/库 rename 和显式重连后再次迁移 | iosql/coordination.py |
| 秒级 calendar 查询漏读 | 保留旧毫秒格式的分区范围，仅在分区裁剪处换算秒级条件，segment 的实际值统计仍按秒比较；无需重写已有 manifest | iosql/engines/sqlite/table.py |
| ui 别名阻塞维护 | ui 与 web 同样排除服务器生命周期租约，业务调用继续通过库级协调 | iosql/cli.py |

## 扩展验证发现并修复

Parquet 未选中表复制后缺少表锁文件，读取现在可以安全重建锁文件。新声明的 Parquet 空表创建 data 目录；旧的空 manifest 在没有任何数据引用时允许缺少 data 目录，实际引用文件缺失仍拒绝。测试区分 Parquet 已持久空表与 SQLite 仅有内存声明的空表，避免使用错误的共同断言。

自审检查了锁内退休与锁外删除的边界、提交后退休失败、目录符号链接、旧句柄声明恢复顺序、所选表新契约优先级、锁失败 FD 释放、共享/独占语义及秒/毫秒统计的隔离。补充 README 和仓库内 skill 契约说明，无需改变公开参数或示例调用。

## 验证结果

- 新增 47 项回归全部通过（35 项迁移/维护回归，12 项锁与平台调用契约验证）。
- 相关测试组 336 项通过，包括真实 PostgreSQL、SQLite/Parquet、CLI、重命名、分区和契约。
- 完整回归：927 passed、6 failed、15 skipped，134.06 秒。六项与原任务基线清单完全一致，没有新增失败、没有隐藏或修改这些断言。
- 修改/新增的 11 个 Python 文件 Ruff 通过；源码校验和与 git diff --check 通过。最终完整回归启动后没有再改 Python 源码。

## 遗留边界

没有 Windows 原机环境。Win32 参数、句柄宽度、锁冲突、错误传播、超时和释放通过可注入系统 API 替身验证；普通操作在无 fcntl 情形通过真实 Linux 文件锁替身验证。没有把这些结果描述成 Windows 原机或断电测试通过。Windows 文件仍同步，目录同步不宣称 POSIX fsync 的同等保证。

退休失败或进程中断可能留下旧内容/任务目录；它们不参与当前视图，清理不得回退为按旧逻辑路径递归删除。六项原有 mysqlite/Web 失败仍属于任务外问题，详见验证记录及 PLAN-083 的基线对照。
