# PLAN-088 README 与 iosql skill 同步

- 日期：2026-10-18（Asia/Shanghai）。
- 状态：已完成。
- 目标：让主 README 和仓库内 `skills/iosql-usage` 准确反映统一迁移、库级协作锁与路径安全相关功能，并移除仍沿用旧并发模型的说明。

## 范围

1. 对照当前迁移实现与设计契约，核对 README、skill 的迁移入口和安全边界。
2. 修正 README 中“运维需手动停写”的旧说明，简述库级共享/独占租约、协作 API 的忙碌行为和仍存在的快照/原子性边界。
3. 更新 skill 的迁移 API 速查、近期示例导航和并发说明，链接到已有详细契约，不重复复制维护细节。
4. 补齐 README 的 13～17 示例导航与三后端能力对照，纠正有序流式读取及迁移示例的对象生命周期说明。
5. 同步 skill 参考资料中的迁移结果字段、异常排查、dry-run 边界、目标空表要求和缓冲/读取流释放要求；补充表说明与字段元数据入口，更新 skill 展示信息。
6. 自审变更，检查 Markdown 本地链接、示例副本一致性、skill 格式和差异格式；本次仅更新文档，不运行业务代码测试。

## 验收标准

- README 与 skill 不再声称备份、恢复、重分区等协作维护必须由用户手动关闭全部连接。
- skill 速查不再把 `Database.migrate_to()` 描述为仅 SQLite → PostgreSQL。
- 新增说明与 `docs/design/migration-api-20261018-unified.md`、`skills/iosql-usage/references/design-contract.md` 一致。
- README 示例覆盖 01～17；后端能力、迁移结果字段和新增异常有明确查阅入口，主文档与参考资料没有相互矛盾的旧说明。
- 结果写入对应 OUTCOME 文档，并登记到 `docs/dev/INDEX.md`。
