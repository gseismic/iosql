# PLAN-069: 大表首屏提速（跳过无用裁剪统计 + 取消与耗时反馈）

- 日期：2026-09-28
- 反馈：用户在真实库（418 万行）Data 页排序后"一直加载中"（实测 14s+），
  认为是"全量读取后再显示"，要求先读部分、按需加载。
- 根因（cProfile 实证）：**不是**全量物化——ORDER BY+LIMIT 下推（PLAN-063）
  已生效，单段裸 SQL top-N 仅 10-40ms。真实瓶颈是 legacy 布局的
  `_legacy_segment_info()` 在**每次查询**对每个 segment 无条件执行
  `_data_file_column_stats()`（24 列 MIN/MAX = 全段扫描，13 段 4.1s）+
  时间/session/COUNT 扫描；这些统计只服务于 WHERE 分区裁剪，**无过滤的
  排序查询完全用不上**。且 UI 首屏实际查询两遍（NULL 探针 + 主查询）。

## 实施项

| # | 层 | 项 |
| --- | --- | --- |
| P1 | 核心 | `_legacy_segment_info(part, lightweight=False)`：轻量模式跳过列统计/时间/session/COUNT 四项数据文件扫描，只保留 part_key 等查询必需字段 |
| P2 | 核心 | `_get_select_part_refs(cases)`：`lightweight = not cases`——无 WHERE 时裁剪统计本来就不会被消费；有 WHERE 时保留（裁剪仍需统计） |
| P3 | 核心 | `_data_file_column_stats` 增加 mtime+size 键的进程内缓存：有过滤的重复查询不再每键击重扫（写入经 mtime 变化自动失效） |
| P4 | 前端 | 加载行显示已耗时（0.1s 步进）+「取消」按钮（AbortController 贯通 IU.api）；新请求自动取消上一请求；取消后回退到上一页数据 |
| P5 | 前端 | "大表未过滤：首页排序可能较慢" 提示文案更新（根因已除，保留提示但不再暗示必然慢） |

## 预期效果

- 首屏（探针+主查询两遍）：~14s → **~1s**（裁剪统计 4.1s×2 → 0）
- 带过滤查询：键击防抖后重复查询命中统计缓存，近似零额外扫描
- 分页本身就是按需加载（keyset 游标），配合取消与耗时反馈

## 验收

1. 新增回归测试：无 where select 不触发 `_data_file_column_stats`；有 where 触发；结果与不走轻量路径一致。
2. 真实库实测 /rows 首屏延迟对比（改前 14.3s / 14.2s warm）。
3. 浏览器验收：加载行出现计时与取消按钮；排序切换即时取消上一请求。
4. 全量回归通过。

## 结果文件

`docs/dev/PLAN-069-fast-first-page-OUTCOME.md`
