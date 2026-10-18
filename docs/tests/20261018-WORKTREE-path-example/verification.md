# PLAN-087 安全迁移与路径示例验证

- 日期：2026-10-18（Asia/Shanghai）。
- 平台：Linux，Python 3.12；未执行 Windows 原机分支。
- 计划：[PLAN-087](../../dev/PLAN-087-path-safety-example-and-commit.md)。

## 主示例

```bash
uv run --frozen --no-sync --offline --with-editable . \
  python examples/17_safe_migration_and_paths.py
```

退出状态为 0。输出如下，其中随机临时目录使用占位符表示：

```text
大小写路径通过：MyDB 正常读写，mydb 保持完整
迁移与备份通过：row-count 校验成功，未选中的合法业务表保留
快照链接已拒绝: 数据库含符号链接或重解析点，拒绝复制: <演示目录>/LinkedSource/ticks/outside
清除链接后备份通过，库外文件保持完整
POSIX 库锁符号链接已拒绝: Too many levels of symbolic links
示例执行完成，运行平台：POSIX（本次不验证 Windows 原机行为）；临时演示目录已清理
```

## 静态检查与副本

- 技能副本同样执行成功（退出状态 0），输出见 [skill-example-output.txt](skill-example-output.txt)。命令为 `uv run --frozen --no-sync --offline --with-editable . python skills/iosql-usage/examples/17_safe_migration_and_paths.py`。
- 主脚本与技能副本的 Ruff 默认规则检查通过。
- `cmp` 核对主脚本与 `skills/iosql-usage/examples/17_safe_migration_and_paths.py` 逐字节一致。
- `cmp` 核对主 README 与技能示例 README 逐字节一致。
- `git diff --check` 通过。

前几轮代码验证沿用 [PLAN-086 结果](../../dev/PLAN-086-windows-paths-and-snapshot-junctions-OUTCOME.md)：330 项通过、6 项 Windows 原机用例跳过。本轮只新增示例及文档，未改动已验证的库实现。
