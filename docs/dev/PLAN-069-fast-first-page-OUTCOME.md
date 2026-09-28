# PLAN-069 实施结果：大表首屏提速（跳过无用裁剪统计 + 取消与耗时反馈）

- 日期：2026-09-28
- 计划：`docs/dev/PLAN-069-fast-first-page.md`
- 结论：**完成，测试通过，真实库延迟 14.3s → 冷 5.2s / 热 1.1s**

## 根因（cProfile 实证）

用户感知的"全量读取后再显示"并非全量物化——PLAN-063 的 ORDER BY+LIMIT
下推已生效（单段裸 SQL top-N 仅 10-40ms）。真实瓶颈：

1. legacy 布局的 `_legacy_segment_info()` 在**每次查询**对每个 segment
   无条件执行 `_data_file_column_stats()`（全部 24 列 MIN/MAX = 全段扫描），
   13 段合计 4.1s，外加 time/session/COUNT 扫描；这些统计只服务于
   WHERE 分区裁剪，**无过滤的排序查询根本不消费**。
2. UI 首屏查询两遍（NULL 探针 + 主查询），成本 ×2。

## 变更

| 层 | 文件 | 内容 |
| --- | --- | --- |
| 核心 | `iosql/engines/sqlite/table.py` | `_legacy_segment_info(part, lightweight=)`：轻量模式跳过列统计/时间/session/COUNT 四项全段扫描，只保留查询寻址必需字段；`_get_select_part_refs` 在无 where 时走轻量路径（裁剪统计此时无消费者）；`_data_file_column_stats` 增加 (path, mtime, size) 键的进程内缓存（写入自动失效），命中/未命中计数器便于观测 |
| 前端 | `static/js/util.js` | `IU.api` 支持 `opts.signal`（AbortController）；主动取消不弹错误 toast |
| 前端 | `static/js/tab-data.js` | 加载行显示实时耗时（0.1s 步进）+「取消」按钮；新请求自动 abort 上一请求；被取消时回退到上一份数据；大表提示文案更新 |

## 验证

- 新增 `tests/engines/sqlite/test_select_lightweight_refs.py`（4 用例）：
  无 where → 0 次统计扫描；有 where → 统计仍计算（裁剪依赖）；
  文件未变时重复查询命中缓存（misses == 0）；轻量/完整路径结果一致。
- 全量回归 `pytest tests/` → **694 passed, 16 skipped**。
- 真实库实测（kline_factor_1m，418 万行 / 13 段 / 839MB 外置盘）：

| 场景 | 改前 | 改后 |
| --- | --- | --- |
| 首屏 冷进程（探针+主查询两遍） | 14.3s | **5.2s** |
| 同条件重复查询（统计缓存命中） | 14.2s | **1.1s** |
| 换排序列 | ~14s | **1.1s** |

- 浏览器验收：排序后加载行显示"加载中 0.2s [取消]"，约 1s 出数；
  分页保持 keyset 按需加载。

## 剩余成本与边界

- 冷进程首屏 ~5s 为物理下限：无索引列的 top-N 排序必须逐段扫描
  （外置盘读 839MB）。如需进一步压缩，后续可为常用排序列建 SQLite 索引
  （`iosql index create` 已支持），或对"时间列排序"做分区倒序早停。
- 统计缓存为进程内：重启后首次查询重算一次（随后命中）。
- 分页本身即按需加载；单页内部分行渲染对 HTTP 单响应无意义，未做流式。
