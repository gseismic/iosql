# 2026 年已知问题历史索引

项目根相对详情目录为 `docs/known-issues/`；条目保留原路径，以下记录结束时状态。

| ID | 标题 | 结束状态 | 结束时间（Asia/Shanghai） | 详情 |
| --- | --- | --- | --- | --- |
| KI-20261021-1806-001 | 带条件查询为 legacy 分区预先执行全列统计，拖慢 CSV 导入前检查 | 已解决（WORKTREE） | 2026-10-21 19:30 | [详情](KI-20261021-1806-001-legacy-query-stat-scans.md) |
| KI-20261105-1626-001 | 强制修改已有表分区后可能漏查及产生重复主键 | 已解决（WORKTREE） | 2026-11-05 18:20 | [详情](KI-20261105-1626-001-force-partition-mixed-layout.md) |
| KI-20261105-1626-002 | 低层写入可向无分区旧表添加分区并造成契约不一致 | 已解决（WORKTREE） | 2026-11-05 18:20 | [详情](KI-20261105-1626-002-low-level-unpartitioned-mismatch.md) |
| KI-20261105-1626-003 | PostgreSQL 可绑定不同分区粒度并产生混合布局 | 已解决（WORKTREE） | 2026-11-05 18:20 | [详情](KI-20261105-1626-003-postgresql-partition-contract.md) |
| KI-20261105-1626-004 | 多日 calendar 分区跨年时可能产生重叠边界 | 已解决（WORKTREE） | 2026-11-05 18:20 | [详情](KI-20261105-1626-004-calendar-multiday-year-boundary.md) |
| KI-20261105-1628-001 | timeseries 的 calendar 字符串导致时间契约错误 | 已解决（WORKTREE） | 2026-11-05 18:20 | [详情](KI-20261105-1628-001-calendar-string-timeseries.md) |
