# PLAN-087 路径安全示例与审查修复提交

- 日期：2026-10-18（Asia/Shanghai）。
- 状态：已完成；示例及验证结果随本轮审查修复提交，提交身份见 Git 记录。
- 用户授权：补充可运行示例并提交代码；覆盖当前工作区中 PLAN-084、PLAN-085、PLAN-086 的相关修复、测试、文档和验证记录。
- 提交约定：中文提交消息，逐项说明变更并列出计划及 OUTCOME 文件；保留用户独立的 `.zcodeignore`。

## 实施方案

1. 新增 `examples/17_safe_migration_and_paths.py`，在脚本自己创建的临时演示目录中展示大小写路径保留、SQLite→Parquet 安全迁移与 SQLite 快照备份、快照拒绝目录联接/符号链接，以及库锁文件拒绝符号链接。
2. Windows 大小写敏感目录通过 `fsutil` 显式启用，junction 通过 `mklink /J` 创建；能力或权限不足时明确跳过相关场景，不把 POSIX 结果当作 Windows 验证。所有构造的链接在 finally 中移除。
3. 同步 `skills/iosql-usage/examples/` 的脚本和 README，补充技能示例导航，执行逐文件一致性检查。
4. 运行示例并记录输出，复核相关改动与现有验证边界，运行必要的静态检查。
5. 生成本计划 OUTCOME，提交前审查暂存范围，用中文提交本轮及前几轮相关工作。

## 步骤

- [x] 实施示例和文档导航，同步技能副本。
- [x] 运行示例、自审及静态检查，核对副本。
- [x] 整理中文提交记录与范围，生成 [OUTCOME](PLAN-087-path-safety-example-and-commit-OUTCOME.md)，随相关修复提交。
