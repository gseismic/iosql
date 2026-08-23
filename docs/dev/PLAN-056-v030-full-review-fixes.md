# PLAN-056: v0.3.0 全量 Review 修复（P0 + C-M1/C-M4）

日期: 2026-08-23
基线: faba3a6（PLAN-054 用户接口定稿）
依据: docs/review/code-review-20260823-v030-full.md

## 目标

按 review 报告第五节"修复路线图 PLAN-A（数据安全）"实施：P0 全部 9 项 + C-M1/C-M4
两条 CLI 数据正确性/原子性缺陷。P1/P2 留待 PLAN-B/PLAN-C 后续轮次。

## 修复清单

| # | 位置 | 修复方案 |
|---|------|---------|
| P0-1 | sqlite/table.py 写路径 + `_increment_segment_info` | 写循环与 compact 共用 `_compact_lock`；`_increment_segment_info` 保留 obsolete 态（与 `_refresh_segment_info` 对齐） |
| P0-2 | mysqlite/table.py `_ensure_columns_on_connection` | `od_fields` 值是类型字符串，白名单直接采用，否则 `suggest_value_type` |
| P0-3 | postgresql/table.py `create_index` | `sql.Identifier` + order 白名单，杜绝 f-string 注入 |
| P0-4 | postgresql/table.py `iter_select_batches` | 单语句命名游标（`WITH HOLD`）流式读取，消除无序 OFFSET 翻页 |
| P0-5 | postgresql/table.py `__init__` | `partition_mode='file'` 抛 `BadArgument`（读写分裂死路径，公开 API 亦不可达） |
| P0-6 | cli.py `restore` | 覆盖前校验目标像 iosql 库（含 meta.sqlite3）且拒绝根/家目录 |
| P0-7 | database.py `rename` | 增加反向包含检查 `source.startswith(target)` 抛 `UnsafeOperation` |
| P0-8 | sqlite/table.py `__init__`/`connect` | 建表入口校验表名（禁 `/`、`..`、`__` 前缀、`.`） |
| P0-9 | mysqlite/table.py + connection_pool.py | `except sqlite3.Error` 统一回滚；`_return_connection` 归还前无条件 `rollback()` |
| C-M1 | cli.py `import` | 用 `result.rows_written`，ignore 模式不虚报 |
| C-M4 | cli.py `repartition` | `replace-source` 用两次 `os.replace` 原子化，备份改为 rename |

## 验收标准

- 全量回归 `python -m pytest tests/ -q` 零失败（基线 345 passed, 16 skipped）。
- 每项 P0 配回归测试（P0-1 需并发写 + compact 压力；P0-3/P0-4/P0-5 PG 侧 mock 验证 SQL 构造）。
- `lsp_diagnostics` 改动文件零新增错误。
- 生成 OUTCOME 文档并中文 commit（docs/ 提交）。
