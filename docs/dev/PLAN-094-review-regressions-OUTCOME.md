# PLAN-094 SQLite 审查回归修复结果

- 日期：2026-10-28。
- 基线：`3425277`；计划：[PLAN-094-review-regressions.md](PLAN-094-review-regressions.md)。
- 范围：六条审查意见，以及自查发现的同一主键、分段裁剪和连接清理路径中的边界。

## 实际变更

1. 从持久化的物理建表列信息取得声明，使用 SQLite 解析类型和内置排序规则；主键归一化分别遵守 INTEGER、NUMERIC、REAL、TEXT、BLOB 亲和性和 BINARY、NOCASE、RTRIM 排序规则。大整数、数字字符串和复合主键按 SQLite 的真实等值语义处理，无法确认的规则不静默猜测。
2. 主键查找直接绑定 SQL 参数，不再把 `[[foo]]` 解析成字段引用，支持 BLOB；不存在物理文件时跳过查找，首次 upsert 和首次 segment ignore 使用一致的类型推断结果。
3. `max_process_connections=1` 的无序流按物理文件暂存结果，释放只读连接后才返回批次；单连接消费者可读写，SQLite/Parquet 各源目标组合的 `save_as` 可完成。其他活跃流耗尽连接预算时快速抛出 `BadArgument`。
4. 单连接流保留原生 `sqlite3.Row`、文件级读取快照和 LIMIT 下推；超时、提前关闭、临时文件写失败和读取截断均释放资源，截断显式报错。
5. 未推断列且尚无数据的时序表允许 `latest`/`asof` 返回空结果，已声明或已推断的字段仍正常校验；列表投影保留混合及重复通配符，排序和 latest 也保持全部列。
6. 分段统计按实际列类型及排序规则判定可信度；历史不可信统计和字段对字段的 session 比较保守扫描。已登记但丢失的 segment 即使缺少分区索引也会报错。

## 验证

- `tests/test_plan094_regressions.py`：66 项定向回归，包含真实 SQLite 对照、SQLite/Parquet 交叉路径和子进程死锁超时保护。
- 相关目录表、时序、Parquet、迁移、原有审查及资源测试：293 通过、1 跳过。
- `ruff check iosql cli examples benchmarks tests/test_plan094_regressions.py` 和 `git diff --check` 通过；变更的 Python 文件按 Python 3.9 语法解析通过。
- 最终完整 `python -m pytest -q -ra`：**1109 通过、87 跳过、125 条警告**，耗时 203.95 秒。跳过项包括未配置独立 `POSTGRES_DSN` 或无法认证的 PostgreSQL、需要 Windows 原机的文件共享/大小写/junction 测试，以及已废弃接口测试；跳过不计为通过。

## 资源与验证边界

单连接无序流的首批需要等待当前物理文件扫描，临时磁盘空间随该文件的结果量增长；内存按批次使用。多文件读取仍是各文件独立快照，并非跨文件原子快照。非二进制排序规则和无法证明可靠的历史统计不参与裁剪，因此可能增加扫描量。

本次使用临时数据库测试；任务开始前已有的 PLAN-092、已知问题文档和测试记录保留在工作区，不纳入本次提交。
