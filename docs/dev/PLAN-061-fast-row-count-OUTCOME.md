# PLAN-061 · 结果记录（OUTCOME）

> 计划：PLAN-061-fast-row-count.md

## 状态：COMPLETE（2026-08-25）

## 交付内容

- `iosql/ui/app.py` 新增 `fast_row_count(table)`：`segments()` 的
  manifest row_count 求和，零文件 I/O；空列表/缺字段/异常回退
  `count_rows()`。`cached_row_count` 与 `/count`（无筛选分支）接入。
- 数据行读取无需改动：Browse 本就是按需读取（keyset 分页，每页
  100–2000 行，翻页才读下一页）——用户建议的模型已存在，本次消除
  的是剩余的行数统计开销。

## 正确性依据（探针验证）

- 删 500 行后 manifest 求和 500 == count_rows；再写 100 行后 600 == 600
  （核心在 delete/write/compact 全程维护 manifest row_count）。
- 单文件模式（无分区无 segment）同样返回带 row_count 的记录。
- 200 段 2000 万行：求和 20,000,000 == count_rows，0.0000s vs 2.66s。

## 效果

| 场景 | 之前 | 之后 |
|---|---|---|
| 2000 万行表 /stats 首次 | 2.66s | **8.6ms** |
| 缓存命中 | 0.17s | 3.3ms |
| 100 表大库渐进统计总耗时 | ~逐表 COUNT 扫描 | 毫秒级/表 |

## 验证

- pytest 全量 **406 passed, 16 skipped**（新增 3 项：单文件表 stats、
  delete 后 stats 同步、/count manifest 求和一致性）。
- 带筛选的 /count 仍流式扫描（语义需要，不受影响）。

## 遗留

- 若未来核心提供 manifest 失真场景（如外部进程绕过 API 直改数据文件），
  fast 路径需加校验开关；当前 UI 定位是本机工具，风险可接受。
