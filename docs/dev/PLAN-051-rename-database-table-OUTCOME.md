# PLAN-051 database/table 重命名功能结果

## 实施结果

1. 增加 `Database.rename(new_uri_or_name, exist_ok=False, overwrite=False)`。
   - 支持 SQLite 目录库重命名。
   - 重命名后刷新当前 `Database.uri`。
   - 对已缓存的表对象先关闭连接，再重新绑定到新库路径。
   - 拒绝内存库、非 SQLite 后端、空目标、同路径目标、目标位于源库内部等不安全操作。

2. 增加 `Database.rename_table(old_name, new_name, exist_ok=False, overwrite=False)` 和 `Table.rename(new_name, ...)`。
   - SQLite 后端移动表目录并更新 `meta`、`table_part`、`table_part_column_range`、`_iosql_segments` manifest 中的 `table_name`。
   - PostgreSQL 后端执行 `ALTER TABLE ... RENAME TO ...`，native partition 模式下同步重命名旧表名前缀的子分区表。
   - `Database` 会维护 `_tables` 缓存，重命名后返回同一个表对象。
   - 对不存在的旧表先报错，避免无意创建空表目录。

3. CLI 增加：
   - `iosql rename-database -s DB --target NEW_DB`
   - `iosql rename-table -s DB -t OLD --new-table NEW`
   - 覆盖已有目标需要 `--overwrite --yes`。

4. 文档和 skill 已更新。
   - `README.md` 增加重命名示例。
   - `TODO.md` 标记完成。
   - `skills/iosql-usage/SKILL.md`、`references/api-reference.md`、`references/usage-patterns.md` 已补充重命名 API/CLI。

5. 新增 `tests/test_rename.py`。
   - 覆盖 database 目录重命名和已打开 table 重新绑定。
   - 覆盖 partition + segment 表重命名后的 manifest、查询、`validate(deep=True)`。
   - 覆盖覆盖目标表和不存在旧表不创建目录。
   - 覆盖 CLI 重命名 database/table。

## 验证

通过：

```bash
pytest tests/test_rename.py
pytest tests/test_rename.py tests/test_cli.py tests/engines/sqlite/test_partition_segment_combinations.py
```

结果：

- `tests/test_rename.py`：5 passed
- 关联回归：66 passed

完整测试：

```bash
pytest
PYTHONPATH=. pytest
```

结果：

- 直接 `pytest` 在收集阶段失败：`tests/test_benchmark_write.py` 无法导入 `benchmarks`。这是本地测试路径问题，与本次改动无关。
- `PYTHONPATH=. pytest` 可进入测试执行；运行 4 分 13 秒后手动中断，已完成 `241 passed, 15 skipped`，中断点在慢速测试执行阶段。

## 后续建议

1. 若希望完整测试默认可跑，应处理 `tests/test_benchmark_write.py` 对 `benchmarks` 的导入路径问题。
2. PostgreSQL 重命名实现未在本地真实 PostgreSQL 服务上做集成验证，后续可在有 `POSTGRES_DSN` 的环境补一条 rename 集成测试。
