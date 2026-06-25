# PLAN-047 PostgreSQL 迁移验证与目标侧基础查询

## 背景

`PLAN-043` 已提供 SQLite iosql 目录库到 PostgreSQL 的基础迁移入口，`PLAN-045` / `PLAN-046` 已补齐大结果集 export 和大文件 import 的批处理管线。当前距离“方便的 sqlite3 到 postgresql 转换，用户无需感知分区”仍有几个缺口：

1. `migrate` 只能执行复制，不能先预览表、行数和批次数。
2. 迁移结果只记录复制行数，缺少源/目标 row-count 校验证据。
3. PostgreSQL 目标表 `select()` 仍是字符串拼接，且没有 `iter_select_batches()`。
4. PostgreSQL `table_names()` 可能暴露 native partition 子表，用户仍会感知底层分区。

## 目标

1. 为 Python API 增加迁移预览：
   - `Database.migrate_to(..., dry_run=True)`
   - `Table.migrate_to(..., dry_run=True)`
2. 为 Python API 增加 row-count 校验：
   - `verify="row-count"`
   - 迁移结果记录 `source_rows`、`target_rows`、`verified`。
3. CLI `iosql migrate` 增加：
   - `--dry-run`
   - `--verify row-count`
4. SQLite 源表提供低成本 `count_rows()`，按物理 segment 执行 `count(*)`，不读取所有行。
5. PostgreSQL 目标表提供：
   - `count_rows(where=None)`
   - `select(..., where=dict/list/text, order_by=..., limit=..., to_dict=True)`
   - `iter_select_batches(...)`
6. PostgreSQL `Database.table_names()` 默认只返回用户逻辑表，不返回 native partition 子表。
7. 增加不依赖真实 PostgreSQL 服务的 fake/mock 测试；真实 PostgreSQL 测试仍由 `POSTGRES_DSN` 控制。

## 非目标

1. 不实现断点续传和 checkpoint。
2. 不实现哈希校验。
3. 不实现 PostgreSQL `latest/asof/explain_select` 完整优化。
4. 不改变 SQLite row segment 到 PostgreSQL native partition 的映射策略。
5. 不改变 `append/ignore/upsert/replace` 写入语义。

## API / CLI 设计

Python：

```python
source = Database("sqlite://db/market.iosql")

plan = source.to_postgresql(
    "postgresql://user:pass@localhost:5432/market",
    dry_run=True,
    batch_size=50_000,
)

result = source.to_postgresql(
    "postgresql://user:pass@localhost:5432/market",
    verify="row-count",
    batch_size=50_000,
)
```

CLI：

```bash
iosql migrate \
  -s sqlite://db/market.iosql \
  --target postgresql://user:pass@localhost:5432/market \
  --dry-run \
  --batch-size 50000

iosql migrate \
  -s sqlite://db/market.iosql \
  --target postgresql://user:pass@localhost:5432/market \
  --verify row-count
```

结果字段：

```json
{
  "dry_run": false,
  "rows_copied": 25000000,
  "batches": 500,
  "verified": true,
  "tables": [
    {
      "table": "ticks",
      "target_table": "ticks",
      "source_rows": 25000000,
      "target_rows": 25000000,
      "rows_copied": 25000000,
      "batches": 500,
      "verify": "row-count",
      "verified": true
    }
  ]
}
```

## 实施步骤

1. 扩展 `TableMigrationResult` / `MigrationResult` 字段和 `to_dict()`。
2. SQLite `Table` 增加 `count_rows()`，迁移时支持 `dry_run` 和 `verify`。
3. Database 级 `migrate_to()` / `to_postgresql()` 透传 `dry_run` 和 `verify`。
4. CLI `migrate` 增加参数、校验参数取值并输出扩展结果。
5. PostgreSQL `Table` 使用参数化 where 构造实现 `select()`、`iter_select_batches()`、`count_rows()`。
6. PostgreSQL `Database.table_names()` 过滤 partition 子表。
7. 增加迁移 dry-run / verify 测试、PostgreSQL 查询 SQL 构造测试、CLI 参数测试。
8. 更新 README、skill、API reference。
9. 运行相关测试、`git diff --check`，实施后 review 并生成 OUTCOME。

## 验收标准

1. `iosql migrate --dry-run` 不创建目标连接，输出表、源行数和批次数估算。
2. `iosql migrate --verify row-count` 会比较源表和目标表 row-count，并在不一致时失败。
3. `Database.to_postgresql(..., verify="row-count")` 返回可审计的源/目标行数。
4. PostgreSQL 目标表支持基础条件查询和批量迭代，不需要用户感知 native partition 子表。
5. 相关测试通过，`git diff --check` 通过。
