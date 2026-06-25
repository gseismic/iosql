# PLAN-050 迁移体验收口与发布前审计

## 背景

`PLAN-043` 到 `PLAN-049` 已连续补齐 SQLite 到 PostgreSQL 迁移、批量导入导出、dry-run、row-count 校验、PostgreSQL 目标侧查询、示例和 calendar/date native partition 边界。用户进一步要求补充 SQLite 与 PostgreSQL 之间的相互转换示例，因此本计划收口双向迁移能力和文档。

## 目标

1. 扫描 README、skill、API reference、usage patterns、troubleshooting 中的 PostgreSQL 迁移旧描述。
2. 补齐 PostgreSQL 源表迁移到 SQLite 目标的批量读取能力。
3. 新增 SQLite ↔ PostgreSQL 双向转换 example。
4. 补齐 API reference：
   - `Database.table_names()` 对 PostgreSQL 返回逻辑表；
   - `count_rows(where=None)`；
   - PostgreSQL native partition 的 calendar/date 支持范围。
5. 整理 README 注意事项中的生产迁移 checklist。
6. 运行最终相关测试和示例 smoke。
7. 生成最终 outcome，作为本轮迁移链路收口记录。

## 非目标

1. 不创建 git commit。
2. 不实现 checkpoint/resume。
3. 不实现 PostgreSQL `latest/asof/explain_select`。

## 验收标准

1. 用户从 examples 能看到 `sqlite -> postgresql` 和 `postgresql -> sqlite` 的双向转换流程。
2. 用户从 README / skill / API reference 能看到同一套迁移建议：dry-run、row-count verify、逻辑表、calendar bound 支持范围。
3. 文档不再声称 PostgreSQL 查询能力与 SQLite 完全一致。
4. 最终相关测试、示例 smoke 和 `git diff --check` 通过。
