# PLAN-046 流式 import 大文件与批量写入管线

## 背景

`PLAN-045` 已经把查询和 CLI `export` 改为批处理输出，但 `iosql import` 仍通过 `_read_rows()` 一次性读取 JSON/JSONL/CSV，再按 `batch_size` 写入。对于金融数据落库，这会限制以下场景：

- vendor 提供的多年 K 线 CSV；
- tick / 逐笔成交 JSONL；
- orderbook 快照或增量回放文件；
- 离线回补包；
- 上游系统导出的财报、公告、新闻事件大文件。

这些文件可能达到数 GB，CLI 应该边解析边写入，而不是把完整文件加载到内存。

## 目标

1. 新增 `_iter_read_rows(file, format_)`，按行/对象迭代读取输入。
2. 新增 `_iter_row_batches(file, format_, batch_size)`，按批返回输入行。
3. CLI `import` 改为消费 `_iter_row_batches(...)`，不再先 `_read_rows()`。
4. JSONL 和 CSV 支持真正流式读取。
5. JSON 对象和 JSON 数组保持兼容；JSON 数组仍需要 `json.load`，文档提示大文件推荐 JSONL/CSV。
6. 输出 summary 保持 `rows_seen`、`rows_written`、`mode`，并新增 `batch_size`。
7. 增加测试证明 CLI import 不再调用 `_read_rows()`，并覆盖 JSONL/CSV 多批写入。

## 非目标

1. 不实现 ijson 等第三方库做 JSON 数组流式解析。
2. 不改变 Python API `table.write()`。
3. 不改变 `import` 的目标表必须已有契约的规则。
4. 不实现自动建表导入。
5. 不改变 `mode=append/ignore/upsert/replace` 语义。

## API / CLI 设计

内部 helper：

```python
for row in _iter_read_rows(path, "jsonl"):
    ...

for batch in _iter_row_batches(path, "csv", batch_size=50_000):
    ...
```

CLI：

```bash
iosql import \
  -s sqlite://db/market.iosql \
  -t ticks \
  -f ticks.jsonl \
  --format jsonl \
  --batch-size 50000 \
  --mode append
```

输出：

```json
{
  "rows_seen": 25000000,
  "rows_written": 25000000,
  "mode": "append",
  "batch_size": 50000
}
```

## 实施步骤

1. 在 `iosql/cli.py` 新增 `_iter_read_rows(...)` 和 `_iter_row_batches(...)`。
2. 保留 `_read_rows(...)` 作为兼容 helper，但让其基于 `_iter_read_rows(...)` 实现。
3. 修改 `import_data(...)`：
   - 先校验 `mode`、`batch_size`、source、table、spec；
   - 遍历 `_iter_row_batches(...)`；
   - 每批调用 `table.write(batch, mode=...)`；
   - 累计 `rows_seen` 和 `rows_written`。
4. 增加 CLI 测试：
   - JSONL 多批导入；
   - CSV 多批导入；
   - monkeypatch `_read_rows` 抛错，证明 import 不依赖全量读取；
   - `batch_size <= 0` 仍拒绝。
5. 更新 README、skill、usage patterns。

## 验收标准

1. JSONL/CSV import 不调用 `_read_rows()`。
2. JSONL/CSV import 按 `batch_size` 多批写入并结果正确。
3. JSON 输入仍保持对象或对象数组兼容。
4. CLI summary 正确包含 `rows_seen`、`rows_written`、`mode`、`batch_size`。
5. 相关测试通过，`git diff --check` 通过。
