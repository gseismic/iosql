# PLAN-075：Parquet 后端

设计：[parquet-20261002-overview.md](../design/parquet-20261002-overview.md)

## 实施步骤

- [x] 1. 注册 `parquet://` URI、可选依赖及目录身份识别。
- [x] 2. 实现 Parquet 物理读写，复用表契约、分区与 segment 逻辑。
- [x] 3. 接入流式查询、校验、整理、删除与迁移；明确拒绝后端不支持的操作。
- [x] 4. 增加示例、README 和使用技能说明，同步仓库技能中的示例副本。
- [x] 5. 运行针对性测试与现有回归，review 并修复问题，记录 OUTCOME。

## 约束

保持既有 API 名称、默认值和 SQLite/PostgreSQL 行为。文档和新增注释使用中文。
只增加可选依赖；不自动接管普通 Parquet 目录，不改动已有用户数据。
未收到提交要求，本轮保留可审阅的工作区变更。

## 实施中评审补充

2026-10-02：文件扫描需阻止并发原子替换；步骤 3 增补共享读取锁与租约，
并覆盖活跃流关闭/写入行为。Parquet 的二进制统计禁用裁剪，避免 manifest
JSON 序列化失败；普通无分区文件缺失时必须显式报错。回归失败将与 HEAD
副本对照，区分新增问题和已有测试问题。

结果：[PLAN-075-parquet-backend-OUTCOME.md](PLAN-075-parquet-backend-OUTCOME.md)。
