# KI-20261021-1806-002：固定结构批写的类型扫描退化

- 创建：2026-10-21 18:06（Asia/Shanghai）。模型：Codex，精确模型 ID 未提供，记为 `model-unknown`。
- 初始状态：未解决。
- 来源：PLAN-090 写入路径 profile 与独立对照。
- 确认版本：`5f6ff5b17ae9e23948d3f3697bf411c1b555c999`。
- 引入依据：`git diff 81c5e0a^ 81c5e0a -- iosql/utils/mysqlite/table.py` 确认 `81c5e0ab8acdac39807f890a64d8306dd9640c9f` 替换固定结构快速路径并加入无条件字段类型扫描。原修复是为保留 SQLite 表达式默认值语义，不能简单回退。

## 触发和影响

`iosql/utils/mysqlite/table.py:528` 在每个内部子批执行 `_union_fields_of_dictlist()`，即使 `ensure_table=False`、`ensure_column=False`，扫描结果也只会在不会执行的 schema 自动创建分支中使用。新文件首次写入先因表不存在失败，再建表重试，重复扫描同批数据。

200 列、20,000 行、每批及 segment 各 5,000 行、只统计 id、WAL/NORMAL、NVMe，3 次中位数：当前 `3,802 行/秒`；诊断进程跳过这遍无用途扫描为 `24,573 行/秒`，再对同构行采用 itemgetter 为 `29,801 行/秒`。profile 的 10,000 行/2 个新 segment 触发约 400 万次类型推断，字段合并累计耗时约占 92%。这些是独立实验，不是已经交付的优化。

候选方向：schema 推断延迟到实际需要建表或补列时执行；同构行恢复快速参数转换，异构行仍保留输入顺序、省略列默认值与事务回滚语义。8 项诊断语义检查通过，但尚未完成正式修复的全部回归，因此状态仍为未解决。

本问题会影响实际导入，但业务当前首要耗时见 KI-20261021-1806-001。研究与原始数据见 `docs/dev/PLAN-090-model-unknown-sqlite-write-research-OUTCOME.md` 和 `docs/tests/20261021-17-WORKTREE/main.json`。
