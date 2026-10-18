# PLAN-087 结果：路径安全示例与审查修复提交

- 日期：2026-10-18（Asia/Shanghai）。
- 计划：[PLAN-087](PLAN-087-path-safety-example-and-commit.md)。
- 状态：示例、同步、自审与提交整理完成；本结果与审查修复一并提交，提交身份以 Git 记录为准。
- 验证：[示例验证与输出](../tests/20261018-WORKTREE-path-example/verification.md)。

## 示例

新增 [17_safe_migration_and_paths.py](../../examples/17_safe_migration_and_paths.py)，全部数据都放在示例自己创建的随机临时目录，结束后自动清理：

1. 在大小写敏感目录中读写 `MyDB`，确认小写兄弟目录 `mydb` 保持完整。Windows 用 `fsutil` 尝试启用该特性，能力或权限不足时明确跳过。
2. SQLite→Parquet 迁移先 dry-run，再执行 row-count 校验；源表名 `events-wal` 和目标未选中的 `.iosql-locks` 业务表均正确保留。SQLite 快照备份可以重新打开并查询。
3. 在示例表中创建指向库外的目录链接。Windows 使用真实 junction，POSIX 使用符号链接；快照拒绝复制，移除链接后可以正常备份，库外文件保持完整。
4. 构造指向外部文件的库锁符号链接，确认无法获得租约且目标未修改。Windows 缺少符号链接权限时明确跳过；检查和清理均限于示例目录。

同步了技能目录中的完整脚本与 README，并补充主 README、示例索引和技能导航。运行方式：

```bash
uv run --with-editable . python examples/17_safe_migration_and_paths.py
```

## 验证结果

- Linux 上主示例与技能副本均运行成功（退出状态 0），覆盖实际迁移、备份、路径大小写、目录链接与锁文件链接拒绝。
- 两份脚本及两份示例 README 的逐字节一致性检查通过；新增脚本 Ruff 默认规则检查通过；差异空白检查通过。
- 本轮库实现未变化，相关测试沿用 PLAN-086 的 330 通过、6 项 Windows 原机用例跳过。Windows 原机及断电验证仍未执行。

## 提交范围

本次用户“写例子，提交”授权更新了前几轮只保留 WORKTREE 的要求。提交包含以下计划的相关实现、测试、设计、使用契约及验证记录：

- [PLAN-084](PLAN-084-model-unknown-migration-review-fixes.md) / [OUTCOME](PLAN-084-model-unknown-migration-review-fixes-OUTCOME.md)：退休路径隔离、快照合法表名、跨平台锁、空目标与空表声明、重连状态、秒级 calendar 查询及 CLI ui 别名修复。
- [PLAN-085](PLAN-085-windows-lock-reparse-points.md) / [OUTCOME](PLAN-085-windows-lock-reparse-points-OUTCOME.md)：Windows 锁文件不跟随重解析点、句柄属性检查及禁止共享删除。
- [PLAN-086](PLAN-086-windows-paths-and-snapshot-junctions.md) / [OUTCOME](PLAN-086-windows-paths-and-snapshot-junctions-OUTCOME.md)：真实路径大小写保留和快照 junction 拒绝。
- [PLAN-087](PLAN-087-path-safety-example-and-commit.md) / 本结果：可运行示例、技能副本和导航，以及示例执行记录。

前几轮计划中的“未提交”是当轮完成时的历史记录，本次提交归属由本计划和 Git 记录说明。用户原有独立文件 `.zcodeignore` 不属于本次提交范围。
