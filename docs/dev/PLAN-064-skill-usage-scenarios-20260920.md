# PLAN-064：系统整理 iosql skill 使用场景与示例

## 背景

当前仓库已经有 `examples/` 和 `skills/iosql-usage/examples/` 两份示例代码，但 skill 的
场景导航没有覆盖所有示例；skill 目录缺少根示例索引和金融示例索引；同时，流式有序查询
已经实现为分片游标归并，参考文档仍保留“复用 `select()` 全量结果”的过期描述。

用户需要通过 skill 快速判断“哪种场景该看哪个入口”，并能直接复制运行示例。示例应围绕
一类问题组织，而不是只展示孤立 API。

## 目标

1. 为 `skills/iosql-usage/SKILL.md` 增加规范 frontmatter、明确触发场景和入口决策树。
2. 对主线、金融、低层 mysqlite 示例逐个给出“解决的问题”说明。
3. 新增一个有代表性的有序大结果集流式读取示例，覆盖
   `query(..., order_by="open_time").iter_rows(...)`。
4. 将 `examples/` 中的示例代码完整同步到 skill 目录；补齐 skill 内的示例索引文档。
5. 修正与当前 API/实现不一致的流式查询参考文档。

## 实施方案

### 1. 场景分层

- 入门：普通 `Table`、`timeseries()`、calendar 日期表。
- 常用生产流：批量缓冲写入、查询计划、有序流式读取。
- 布局维护：segment、晚到数据、layout report、repartition、doctor/CLI。
- 已有库与迁移：重新打开、SQLite → PostgreSQL、双向 roundtrip。
- 专项：金融数据和低层 mysqlite。

### 2. 示例组织

- 新增 `examples/13_streaming_ordered_query.py`。
- 完整复制到 `skills/iosql-usage/examples/13_streaming_ordered_query.py`。
- 新增/同步 `examples/README.md` 和 `examples/finance/README.md` 到 skill 示例目录，
  让 skill 内部资源自洽。
- 不复制 `examples/runtime/` 作为源码资源；运行时数据仍由脚本按需生成并清理。

### 3. Skill 与参考文档

- `SKILL.md` 使用第三人称 description 和动词开头的操作说明，保持核心内容精简；
  以场景表引用示例，不在 skill 中重复维护整份脚本代码。
- `api-reference.md`、`usage-patterns.md` 明确 `iter_select*` 和
  `query().iter_rows()/iter_batches()` 的有序流式行为、限制和连接预算。

## 验收标准

- `SKILL.md` 存在有效 YAML frontmatter，包含具体触发短语；引用的资源全部存在。
- 主线 01～13、finance、mysqlite 和 mysqlite-scenarios 的源码在两处目录中逐文件一致。
- 新示例在仓库根目录和 skill 目录各自可运行，且展示首条结果先于全部结果消费。
- `python -m compileall -q iosql examples skills/iosql-usage/examples` 通过。
- 示例索引、skill 场景导航和参考文档不再保留与当前实现冲突的描述。
- 相关测试和示例冒烟通过；生成运行时数据不纳入提交。
