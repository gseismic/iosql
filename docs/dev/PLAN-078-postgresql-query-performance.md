# PLAN-078 PostgreSQL 时序查询与性能诊断

- 日期：2026-10-02
- 状态：实施、评审、本机测试与基准完成
- 设计：`docs/design/postgresql-query-performance-20261002-overview.md`

## 评审结论与实施范围

原设计方向成立，但需补全 inclusive end/as_of、latest 同时间决胜与输出顺序、原生日期类型转换、EXPLAIN 脱敏与实际扫描判定、索引归属及重复输入更新语义。全部三个实施阶段在本计划内完成，不以仅补 query() 作为验收。

## 实施步骤

1. 更新设计，固定 PostgreSQL 14+、边界/NULL/排序与脱敏契约；查询语义复用独立公共模块，避免依赖 SQLite 文件实现。
2. 实现 query/latest/asof；SQL 下推筛选、投影、排序、limit 与分组取最新值；基准驱动补充有 symbol/time 前缀索引时的 LATERAL 单组最新值查询，其他分组使用 DISTINCT ON；沿用命名游标，支持提前关闭、异常和显式事务。
3. 扩展 QueryPlan；实现默认不执行的 EXPLAIN JSON、显式 ANALYZE/BUFFERS 与可选原始计划，记录关系、访问路径和估算/实际指标。
4. 建表前检查分区列属于主键；绑定已有表检查类型、主键顺序、RANGE 方式与单分区键。失败不污染已绑定契约。
5. 增加复合 B-tree 索引与安全删除；复用等价主键索引，校验列顺序/方法/有效性/归属，父表索引覆盖已有和未来分区。
6. upsert_all 改为按输入字段集合的 ON CONFLICT 增量更新；保留缺字段、显式 NULL、重复主键顺序合并和事务回滚语义。
7. 编写真实 PostgreSQL 示例与百万/千万行可复跑基准，比较平表/分区与有无索引、时间窗/仅 symbol、latest/asof、小/大批 symbol、全表流式读取及 RSS。
8. 自审并补回归测试；强化原生 calendar 日期/时间的负时区与小时边界，刷新过时的流式回调；全量回归中原有 NDJSON 图表与计数预算测试失败以独立 HEAD 工作树复核并记录，在独立 PostgreSQL 14 实例执行集成验证及相邻 SQLite/partition/mysqlite 回归；运行示例和基准，保存环境与结果。
9. 更新 README、仓库 skill 资料和结果文件；提交本次文件，中文提交消息逐条记录变更与计划/结果位置；推送。

## 验收证据

- PG/SQLite 相同数据的 query/latest/asof 边界、字段及顺序对照，原生 date/timestamp/timestamptz 和 calendar text 覆盖。
- EXPLAIN 默认无实际指标，ANALYZE 有实际耗时、buffer；脱敏计划不包含谓词/SQL/参数；latest 计划描述其真实 SQL。
- 真实分区裁剪、索引继承、错误契约绑定、游标提前关闭及字段级更新/事务回滚测试。
- 100 万/1000 万行 JSON 与中文报告，含首批耗时、总吞吐、RSS、PG/硬件/分区/索引/缓存条件。
- `docs/dev/PLAN-078-postgresql-query-performance-OUTCOME.md` 逐项记录已实现能力及验证结果。

## 工作区约束

- 初始 `.zcodeignore` 为用户未跟踪文件，不纳入提交。
- 原设计文件为用户提供的未跟踪文档，按请求评审修订并纳入本次提交。
- 当前 master 已领先远端 2 个提交，保持这些提交及现有变更。
