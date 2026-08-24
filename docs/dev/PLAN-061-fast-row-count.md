# PLAN-061 · 行数统计零 I/O 化（manifest 求和）

## 问题（承接 PLAN-060 用户反馈）

"开始只读取特定行数就可以吧，也显示不了那么多，用户翻页时再读。"

- 数据行读取：Browse 本就是该模型（keyset 分页，每页 200 行，翻页才读）。
- 剩余开销在**行数统计**：`count_rows()` 对每个物理 segment 执行 COUNT(*)，
  必须打开全部数据文件；2000 万行实测 2.7s（冷缓存/HDD 更差），而
  PLAN-060 的渐进统计仍会对每张表触发一次。

## 发现

`table.segments()` 返回的 manifest 记录**已含每段 row_count**，
且写入/删除/compact 全程由核心维护（探针验证：删 500 行后 manifest
求和 500 == count_rows；单文件模式同样带 row_count）。
求和即精确总行数，**零文件 I/O**：2000 万行 0.0000s vs 2.66s。

## 变更

1. `iosql/ui/app.py` 新增 `fast_row_count(table)`：
   - `segments()` 求和（manifest 级，不打开数据文件）
   - 空 segment 列表 / 记录缺 row_count / 异常 → 回退 `count_rows()`
2. `cached_row_count` 与 `/count`（无筛选分支）改用 fast 路径；
   带筛选的计数仍流式扫描（语义需要，无法避免）。
3. 测试：分段表/单文件表 stats 行数正确；delete 后 stats 同步；
   fast 与 count_rows 一致性。

## 验收
- 大库 stats 毫秒级返回且数值与 count_rows 一致。
- 既有测试全绿。
