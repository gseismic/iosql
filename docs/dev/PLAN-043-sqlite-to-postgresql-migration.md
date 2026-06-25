# PLAN-043 SQLite 到 PostgreSQL 易用迁移接口

## 背景

当前 iosql 主 API 同时暴露 `sqlite://` 和 `postgresql://` URI，但从已有 SQLite 目录库迁移到 PostgreSQL 的用户路径不清晰：

1. 单表可以尝试使用 `repartition_to("postgresql://...")`，但名称表达的是重分区，不符合“迁移到 PostgreSQL”的心智。
2. PostgreSQL 引擎缺少 `use_contract()`、`spec()`、`write()` 等主 API 契约方法，不能稳定作为 `Database.table(..., spec=...)` 的目标。
3. `repartition_to()` 当前按 logical partition 聚合读行，海量 K 线场景容易一次读入过多数据。

用户目标是：已有 SQLite 版 iosql 数据，希望转成 PostgreSQL 版，并且有容易使用的接口。

## 目标

1. 提供易用接口：
   - `source_table.migrate_to(target_uri, ...)`
   - `source_db.migrate_to(target_uri, tables=None, ...)`
   - `source_db.to_postgresql(target_uri, ...)` 作为语义别名。
2. 保留表契约：主键、列类型、partition、order_key、time/session 等元数据应传给目标表。
3. PostgreSQL 目标支持主 API：
   - `Database("postgresql://...").table(name, spec=...)`
   - `table.write(rows, mode=...)`
   - `table.spec()`
4. 针对金融大数据，源 SQLite 按物理 segment / SQLite 文件批量读取，不一次性加载全库。
5. 提供 CLI：
   - `iosql migrate -s sqlite://... --target postgresql://...`
6. 增加无需真实 PostgreSQL 服务的单元测试，使用 mock target Database 验证迁移批次、契约和统计；真实 PostgreSQL 测试继续由 `POSTGRES_DSN` 环境决定是否运行。

## 非目标

1. 不实现 PostgreSQL 上完整的 `latest/asof/explain_select` 优化。
2. 不自动删除或修改源 SQLite 库。
3. 不在本轮做跨库增量同步、断点续传或校验哈希。
4. 不把 SQLite row segment 映射成 PostgreSQL segment；PostgreSQL 侧以表契约和 native partition 为主。

## API 设计

### 单表

```python
source = Database("sqlite://db/market.iosql")
ticks = source.table("ticks")

result = ticks.migrate_to(
    "postgresql://user:pass@host:5432/market",
    batch_size=50_000,
    mode="append",
)
```

### 整库

```python
source = Database("sqlite://db/market.iosql")

result = source.migrate_to(
    "postgresql://user:pass@host:5432/market",
    tables=["daily_bars", "kline_1m"],
    batch_size=50_000,
)

# 语义别名
source.to_postgresql("postgresql://user:pass@host:5432/market")
```

## 实施步骤

1. 新增迁移结果类型：
   - `TableMigrationResult`
   - `MigrationResult`
2. SQLite `Table` 增加：
   - `iter_rows(batch_size=...)`
   - `migrate_to(target_uri, target_table=None, mode="append", batch_size=...)`
3. 高层 `Database` 增加：
   - `migrate_to(target_uri, tables=None, mode="append", batch_size=...)`
   - `to_postgresql(target_uri, ...)`
4. PostgreSQL `Table` 补齐主 API 契约：
   - `use_contract()` / `use_spec()`
   - `spec()`
   - `write()`
   - `do_insert_all()`
   - table-level composite primary key
   - 基础 SQLite 类型到 PostgreSQL 类型映射
5. CLI 增加 `iosql migrate`。
6. 测试：
   - mock target 验证 `Database.migrate_to()` 多表迁移。
   - mock target 验证 SQLite source 按 batch 迁移，不一次性读全库。
   - PostgreSQL 引擎无服务环境下至少可编译和 mock 主 API 行为。
7. 更新 README、skill 和 API reference。
8. 运行相关测试并生成 OUTCOME。

## 验收标准

- 用户可以用一行 `Database("sqlite://...").migrate_to("postgresql://...")` 迁移库。
- 单表迁移和整库迁移都返回行数、批次数、表名等结果。
- 迁移不修改源库。
- 对海量 K 线，迁移以 `batch_size` 为上限逐批写目标。
- 目标 PostgreSQL 表能通过 `Database.table(..., spec=...)` 使用主表契约创建和写入。
