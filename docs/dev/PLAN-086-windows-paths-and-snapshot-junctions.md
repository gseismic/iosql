# PLAN-086 Windows 路径大小写与快照重解析点防护

- 日期：2026-10-18（Asia/Shanghai）。
- 状态：已完成（WORKTREE）；Windows 原机用例尚未执行。
- 范围：修复真实文件访问路径被 `normcase()` 小写化，以及快照遍历跟随 Windows junction 的两项 P2 问题。
- 工作区：延续 PLAN-084、PLAN-085 的未提交改动；不提交或推送。

## 实施方案

1. `resource()` 和 `physical_path()` 保留 `realpath()` 返回的实际大小写，只有库级锁身份使用 `normcase()`。库控制记录位置和实际代际目录都从保留大小写的路径派生。
2. 快照根目录及每个子项在读取、过滤或递归之前执行不跟随链接的 `lstat()`，同时拒绝 POSIX 符号链接和 Windows `FILE_ATTRIBUTE_REPARSE_POINT`。目录、普通文件的分类复用该次读取的类型信息，不通过跟随链接的属性查询决定是否递归。
3. 测试覆盖大小写目录存在与不存在、两后端真实访问、持久代际路径，以及正常/悬空链接、junction、其他重解析点在根目录和递归层级的拒绝。
4. Linux 上提供 Windows 路径规范化和属性的确定性模拟；Windows 条件用例通过 `fsutil` 启用目录大小写敏感，并通过 `mklink /J` 创建真实 junction，避免只依赖 Linux 的符号链接用例。

## 步骤与验收

- [x] 补充可复现两项问题的测试：修复前 16 失败、1 通过、4 项 Windows 原机用例跳过。
- [x] 实施路径分离与快照入口检查。
- [x] 自审并运行锁、快照、迁移及相关契约回归：330 通过、6 项 Windows 专属用例跳过。
- [x] 更新设计、开发契约、索引及对应 [OUTCOME](PLAN-086-windows-paths-and-snapshot-junctions-OUTCOME.md)，说明原机验证边界。

## 验证边界

当前主机为 Linux，Windows 的真实大小写敏感目录和 junction 用例只能在 Windows 执行；能力或权限不足时明确跳过，不计为通过。快照入口的属性检查不宣称抵抗未参与库锁协议的外部进程在检查后的任意恶意替换。
