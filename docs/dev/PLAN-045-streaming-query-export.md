# PLAN-045 流式查询与大结果集导出

## 背景

当前 `select()`、`query().to_list()` 和 CLI `export` 会把查询结果一次性加载到内存。对于金融海量数据，这会限制以下场景：

- 多年分钟 K 线导出；
- tick / 逐笔成交批量回测读取；
- orderbook 快照或增量跨时间窗口扫描；
- 迁移前后大表抽样或全量校验；
- 新闻、公告、财报事件按时间范围批量导出给下游。

`PLAN-042/044` 已经解决了 segment compaction 与旧文件清理，`PLAN-043` 已经有物理文件级 `iter_rows(batch_size=...)` 用于迁移。但用户层还缺按 where/order/limit 语义查询的大结果集流式 API。

## 目标

1. 新增 `table.iter_select(...)`，按查询语义迭代返回行，不要求调用方一次性持有全部结果。
2. 新增 `table.iter_select_batches(..., batch_size=...)`，按批返回查询结果。
3. `table.query(...).iter_rows()` 不再退化为 `to_list()`，而是使用流式查询。
4. `table.query(...).iter_batches(batch_size=...)` 支持时序查询批处理。
5. CLI `export` 改为批处理写文件，避免 JSONL/CSV/JSON 导出时先构造完整 rows 列表。
6. 保持 `select()`、`query().to_list()`、CLI `query` 兼容既有行为。
7. 增加测试覆盖批大小、limit、字段投影、CLI JSONL/CSV/JSON 导出。

## 非目标

1. 不实现外部归并排序或临时磁盘排序；带全局 `order_by` 的跨 segment 查询仍可能需要内存排序以保证正确性。
2. 不改变 `select()` 的返回类型。
3. 不实现 PostgreSQL 流式 cursor。
4. 不改变 `latest()` / `asof()` 的内部算法。
5. 不改变 import 读取全文件的行为；流式 import 可作为后续目标。

## API 设计

```python
for row in table.iter_select(
    fields="symbol,ts,price",
    where={"field": "ts", "op": "between_and", "value": [start, end]},
    batch_size=50_000,
    to_dict=True,
):
    ...

for batch in table.iter_select_batches(
    "*",
    where=None,
    batch_size=50_000,
    to_dict=True,
):
    ...

for batch in ticks.query(start=start, end=end).iter_batches(batch_size=50_000):
    ...
```

CLI：

```bash
iosql export -s DB -t ticks -o ticks.jsonl --where @where.json --batch-size 50000
iosql export -s DB -t ticks -o ticks.csv --format csv --batch-size 50000
```

## 实施步骤

1. 在 SQLite `Table` 中新增：
   - `iter_select(...)`
   - `iter_select_batches(...)`
   - 流式 SQL 查询和单文件分批查询 helper。
2. 更新 `_TableQuery.iter_rows()` 和新增 `_TableQuery.iter_batches(...)`。
3. CLI 增加 `--batch-size` 并让 `export` 批处理写文件：
   - JSONL：逐行追加；
   - CSV：首批写 header，后续追加行；
   - JSON：流式写数组，避免先构造完整列表。
4. 更新 README、skill、API reference 和 usage patterns。
5. 测试：
   - Python API 批量迭代；
   - `query().iter_batches(...)`；
   - `limit` 跨 batch 生效；
   - CLI JSONL/CSV/JSON 导出。

## 验收标准

1. `iter_select_batches(batch_size=N)` 每批不超过 N。
2. `iter_select(...)` 与 `select(..., to_dict=True)` 在无 `order_by` 和有 `order_by` 场景结果一致。
3. `query(...).iter_batches(...)` 与 `query(...).to_list()` 结果一致。
4. CLI export 不再调用 `_query_rows()` 先获取完整列表。
5. 相关测试通过，`git diff --check` 通过。
