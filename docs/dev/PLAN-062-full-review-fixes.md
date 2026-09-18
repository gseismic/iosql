# PLAN-062 全量 Review 修复（P0/P1/P2）

- 计划日期：2026-09-18
- 基线：HEAD `0da5b0c`；review 报告：`docs/review/code-review-20260918-full.md`
- 目标：修复 review 报告中全部 P0/P1，并修复 P2 中属正确性/安全/契约明确缺陷的条目；
  剩余纯性能大改造（latest 物化、非 segment 全表聚合缓存等）若风险高则单独记录，不阻塞本计划。
- 原则：
  1. 不改冻结接口的参数名/默认值（PLAN-054 冻结边界）；新增校验导致的拒绝属于修 bug。
  2. 每个修复必须有回归测试；改动后跑相关测试 + 全量测试。
  3. 行为变更需同步 SKILL/README（由统一收尾任务完成，避免并行编辑冲突）。

## 批次 A：P0（数据正确性/安全）

| # | 修复项 | 范围 |
|---|--------|------|
| A1 | SQLite 并发读回滚 delete/truncate（auto_commit 竞态） | engines/sqlite/table.py + mysqlite |
| A2 | compact 与 delete/truncate 互斥（防删行复活） | engines/sqlite/table.py |
| A3 | calendar fmt 白名单 + 分区文件路径 containment | partition/calendar.py + spec.py + engines/sqlite/table.py |
| A4 | CLI restore 目标身份判定修正（真实库可覆盖、防伪造、防 symlink） | cli.py |
| A5 | CLI restore backup/target 重叠自毁防护 + 原子替换 | cli.py |
| A6 | Database.rename overwrite 目标校验 + 根/家目录拒绝 + 可回滚 | database.py |
| A7 | PG delete falsy where 全表删除 | engines/postgresql/table.py |
| A8 | PG 列类型 DDL 注入（白名单映射） | engines/postgresql/table.py |
| A9 | mysqlite insert_all 表达式默认值字面量写盘 | utils/mysqlite/table.py + sql.py |
| A10 | UI repartition “预览”实际写库（去掉假 dry_run / 明确确认） | ui/app.py + ui/static/js/tab-maintain.js |

## 批次 B：P1（正确性/契约）

- SQLite：partition setter 守卫；select/iter_select 未知 kwargs 拒绝；缺失 segment 文件统一行为且读路径不隐式建文件；validate FD 泄漏；select(to_dict=False) 投影返回 tuple；spec= 绑定已有表生效。
- 核心：`Partition.calendar` 传 every；浮点分区键归一；`migrate_to(dry_run=True)` 不落盘；`Database.rename` 身份校验；`4month/4year` roundtrip；TimeColumn tz；force_partition/spec=conflict 透传。
- CLI：migrate 同库守卫；export 原子写；导入 BOM/坏行/CSV restkey 处理；部分写入汇总；dry-run 解析校验。
- UI：NULL 排序列分页；maintain/explain 响应解包；导出文件名转义；/rows 依赖核心 LIMIT 下推；上一页回退；import_buffer 文档/测试对齐。
- PG：字符串 fields 引号化；批量列集合并集/NULL 补齐；commit 参数生效与失败回滚；timestamptz 边界带 offset。
- mysqlite：单行 ensure_column None 类型；drop_table 缓存失效；wal=False；close/return 竞态；嵌套 checkout 死锁；commit=False/auto_commit=False 丢写；进程预算 min()；timeout 被 busy_timeout 覆盖。

## 批次 C：P2 与旧报告遗留

- 各模块 P2 清单见 review 报告第五节；旧报告仍未修项（U-M1/U-M2/U-M3/U-M4/S-M1(已并入 S1-4)/S-M3/S-M4/S-M8/S-M11/P-M1/P-M3）按可行性修复或在 OUTCOME 记录明确保留理由。
- 性能项：优先做 select ORDER BY + LIMIT 下推（同时解决 UI /rows 物化）；latest 物化与非 segment 聚合若改动过大，记录为独立后续计划。

## 验收标准

1. 全量 `pytest tests/ -q` 通过（含新增回归测试），无新增 skip。
2. review 报告中 P0/P1 条目逐条有对应测试或修复证据。
3. `examples/` 冒烟通过；`skills/iosql-usage/examples` 不回归。
4. 产出 `docs/dev/PLAN-062-full-review-fixes-OUTCOME.md`，逐项记录修复与保留项。
