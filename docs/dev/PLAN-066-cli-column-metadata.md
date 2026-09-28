# PLAN-066: CLI 暴露列元数据（describe / update-metadata）

- 日期：2026-09-28
- 依据：`docs/design/column-domain-20260928-api.md`（PLAN-065 已实施 Python API，
  CLI 尚未暴露；本计划按该设计 §4.2/§4.3 的语义补齐命令行入口）

## 背景

PLAN-065 为 `TableContract` 增加了 `description` / `column_descriptions` / `column_ranges`，
并提供 `Table.describe()`（JSON 安全读取）与 `Table.update_metadata()`（SQLite 增量更新）。
两个能力目前只能通过 Python 调用，命令行用户与 LLM 工具链无法使用。

## 实施项

### P1 `iosql describe -s DB -t TABLE`

- 输出 `Table.describe()` 的 JSON（表名/表介绍/主键/逐列 类型·解释·值域）。
- 只读操作，与 `iosql spec` 并列放置（spec 输出原始契约，describe 输出面向人/LLM 的规范结构）。
- 表不存在 → `_fail`；输出经 `_echo_json`。

### P2 `iosql update-metadata -s DB -t TABLE [选项]`

映射设计 §4.2 的"省略 = 不修改 / `None` = 清除"语义：

| 选项 | 设计语义 |
| --- | --- |
| `--description TEXT` | 设置表介绍（省略 = 不修改） |
| `--clear-description` | 清除表介绍（`description=None`） |
| `--column-desc COL=TEXT`（可重复） | 增量更新字段解释；`COL=`（空文本）= 清除该列解释 |
| `--clear-column-descs` | 清空全部字段解释（`column_descriptions=None`） |
| `--column-range COL=LOW:HIGH`（可重复） | 声明闭区间值域；`LOW/HIGH` 支持数值与 `inf`/`+inf`/`-inf` |
| `--clear-column-ranges` | 清空全部值域（`column_ranges=None`） |

- 互斥校验：`--description` 与 `--clear-description`、`--column-desc` 与
  `--clear-column-descs`、`--column-range` 与 `--clear-column-ranges` 不得同用。
- 全部省略 → `_fail`（无操作）。
- 值域解析失败（非法数值/low > high）→ `_fail`，消息含列名与修复示例。
- 成功输出更新后的 `describe()` JSON（`update_metadata()` 返回值）。
- 沿用 `_guard_command_errors`：`BadArgument` 等核心异常转 `_fail`（消息含列名）。

### P3 文档

- README CLI 命令表新增 `describe` / `update-metadata` 两行。
- `skills/iosql-usage/SKILL.md` 若有命令清单则同步。

## 不做

- `create-table` 建表时声明元数据的 CLI 参数（设计建议建表走 Python API；
  旧表增补已由 update-metadata 覆盖）。
- PostgreSQL 的 update_metadata（设计明确不在范围）。

## 验收

1. `pytest tests/test_cli.py`：新增 describe 输出形态、update-metadata 设置/清除/
   互斥/非法值域用例，全部通过；全量回归无失败。
2. 手工冒烟：对演示库执行 describe 与 update-metadata，输出符合设计 §4.3 JSON 形态。

## 结果文件

`docs/dev/PLAN-066-cli-column-metadata-OUTCOME.md`
