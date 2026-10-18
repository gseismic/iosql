# PLAN-083 结果：统一迁移、目标分区调整与安全发布

- 时间：2026-10-18 12:43（Asia/Shanghai，实施和验证完成）。
- 执行模型：Codex（GPT-6，精确模型 ID 未提供）。
- 版本：WORKTREE；基准 `7a0f87fd1289eb551896b9d38c509cd2c4edc311`。未执行提交或推送，用户 `.zcodeignore` 保留。
- 计划：[PLAN-083](PLAN-083-model-unknown-safe-migration.md)。
- 设计：[统一迁移](../design/migration-api-20261018-unified.md)。
- 验证：[验证说明与原始记录](../tests/20261018-12-WORKTREE/migration-verification.md)。

## 已实施

两个作用域统一调用：

```python
source.migrate_to(
    "parquet://db/hourly.iosql",
    tables={"bars": "hourly_bars"},
    specs={"bars": {"partition": "duration(ts,every=1hour)"}},
    verify="row-count",
)

bars.migrate_to(
    "parquet://db/hourly.iosql",
    specs={"bars": {"partition": "duration(ts,every=1hour)"}},
    verify="row-count",
)
```

- Database 的范围为整库/多表，Table 的范围仅当前表。None/list/dict 共用名称及意义；新参数 specs/temp_dir/lock_timeout 一致。保留旧 target_table、第二位置目标名和 to_postgresql，归一化后进入共同迁移器。
- specs 按源表名指定目标分区/段布局，支持完整契约或布局覆盖字典。支持新增/移除分区、修改分区列/间隔/种类与段大小；完整契约保留列、主键及时间含义。已有目标必须兼容最终目标契约，replace 不解释为清空。
- 源共享预检先校验源选择、布局和时区；随后按稳定顺序取得源/目标独占租约并重建计划。复制期间禁止协作读写；普通操作立即 DatabaseBusy，迁移有总等待期限。流式查询从首次消费至关闭都持租约，PG commit=False 事务持续持租约。
- 本地默认临时工作区位于源逻辑目录，PG 源使用系统临时目录。完整目标视图包含未选中表，最终代际在目标文件系统准备，关闭暂存连接、校验和 fsync 后，先持久化提交决定，再原子更新 physical_root/generation/COMMITTED。旧物理路径不复用，避免旧 WAL 连接触及新库。
- 任务状态覆盖前按 ID 归档到稳定协调空间，后续旧代际清理不删除已提交历史；错误提供恢复位置。新目录及其父项同步保证记录先于后续指针覆盖。
- 缓冲写入的 append/flush 也受屏障约束，with 上下文持租约；flush 失败保留输入，不自动重试。显式 PG URI connect_timeout 保留。
- 读写均接入恢复屏障；已决定提交的中断按 device/inode/marker 重放指针。dry-run 只持源共享锁、不连接或锁定目标、不建临时业务目录、不自动恢复待恢复的源。
- PG 源快照、正式目标与协调会话分别由迁移器持有。暂存 schema 完成后，全部目标变更、真实行数验证、任务状态和 generation 在同一专用事务 COMMIT。提交回复不确定时由任务锁和稳定提交记录确认，不修改调用方未提交事务。
- SQLite 缓存跨代际重建；旧 PG 绑定对象只读恢复契约并确认物理关系，未选中对象也保持可用。新打开 PG 表仍显式绑定契约。
- 返回类型及旧字段保留，追加 rows_written、published、operation_id、源/目标契约与本次新增目标分区。错误保留原始链、阶段与部分进度；部分复制不表示正式发布。
- repartition_to 兼容返回类型和连接配置，复用安全流式流程；backup/restore、已发布库重命名与 CLI 路径适配代际。CLI 普通文件扫描持完整共享租约，restore 在独占锁内重新检查覆盖授权。嵌套维护清理推迟到最外层租约释放后。
- 示例 11/12/16、README、仓库内 iosql skill 文档及副本同步。示例专用清理同时处理演示库控制状态，允许重复执行。

## 审查中修复的主要问题

1. 目录同名替换与空闲 SQLite WAL 连接冲突：改用完整物理代际及单一指针发布。
2. 已打开 Database 再刷新时关闭了已刷新 Table：按 generation 检查，PG 保留实例绑定。
3. 普通空表的延迟建表行为不应被改写：只保证迁移目标空表契约持久化，当前已声明源空表纳入迁移。
4. PG 默认批次提交/读取会破坏事务所有权：正式事务专用适配器禁止分批自动提交，源命名游标复用源锁连接。
5. 全局完成标记与 generation 分开更新存在可见性窗口：同一控制替换或 PG 事务共同提交。
6. 数值时间改 calendar 时的单位猜测和 PG 文本边界：明确 ms/s 并保留数值，重开后继续正确路由与查询。
7. 参数错误不应先触目标连接：增加源共享预检，并在有序独占锁内复核源变化。
8. 普通写入、CLI 原始文件扫描、流消费与恢复权限竞态：补全租约与恢复检查，覆盖授权在锁内复核。
9. 清理仍可能处于外层维护租约内：通过 after_leases 统一延后，锁不等待递归删除或暂存 schema 清理。

## 计划调整

物理发布由早期逐表目录移动修订为完整代际指针，扩大了身份识别、缓存和维护入口适配范围；用户后续要求加入迁移时分区调整，因此补充了 specs 和时间单位处理。每项实质变更均先补充计划/设计，再实施。没有引入持续变更捕获、并发迁移读写或任意列类型转换。

## 验证与遗留

最终完整回归：**880 passed、6 failed、15 skipped**。六项失败均在独立基线副本复现；它们属于三个 mysqlite 旧断言与三个 Web JSON/流输出旧断言，没有修改或跳过以掩盖失败。本任务的安全迁移与相关维护针对性组 176 项通过；最终新增安全迁移组独立重跑 **52 passed**，最后完整回归也包含源恢复错误定位断言。修改的 40 个 Python 文件 Ruff、git diff --check、8 组示例副本一致性通过。完整测试已完成，没有将六项基线失败或十五项原有 skip 记为通过。

新增、变更代码的校验和在测试目录 source-hashes.txt；完整回归的最终记录是 full-regression.txt。最初环境缺少发布测试所需工具及一次并行使用相同 PG 数据库造成的干扰已单独纠正；最终证据使用完整依赖并隔离执行。

主要限制：需要完整目标副本的额外空间；本地父目录须能创建/访问协调文件，旧版或原始文件访问不参与锁。PG 需要专用协调连接及管理 schema 权限，新打开 PG 对象仍需绑定契约。失败暂存保留供排查；完整复制和合并仍有各自 mode 的重试语义，提交不确定时不能盲目再次 append。
