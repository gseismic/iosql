# PLAN-051 database/table 重命名功能

## 背景

`TODO.md` 要求增加 database/table 重命名功能。当前 iosql 的 SQLite 引擎以目录表示 database、以子目录表示逻辑表；PostgreSQL 引擎以真实关系表表示逻辑表。需要在主 API 中提供统一入口，并保证已打开对象的状态在重命名后可继续使用。

## 目标

1. 增加 database 重命名能力：支持 SQLite 目录库重命名，并刷新当前 `Database` 实例 URI。
2. 增加 table 重命名能力：支持 `Database.rename_table(old, new)` 与 `Table.rename(new)`。
3. SQLite table 重命名需要移动表目录，更新表内 manifest 的 `table_name` 字段，并刷新已打开表对象的路径、缓存和 table name。
4. PostgreSQL table 重命名需要对主表执行 `ALTER TABLE ... RENAME TO ...`，并在 native partition 模式下同步重命名当前表前缀下的子分区表。
5. CLI 增加 `rename-database` 和 `rename-table` 命令。
6. 增加覆盖 SQLite database/table 重命名的回归测试；PostgreSQL 在缺少运行环境时只保留代码层实现。

## 方案

1. 在 `Database` 增加：
   - `rename(new_uri_or_name, exist_ok=False, overwrite=False)`：仅 SQLite 支持，移动目录并刷新实例。
   - `rename_table(old_name, new_name, exist_ok=False, overwrite=False)`：委托表对象执行重命名并维护 `_tables` 缓存。
2. 在 `BaseTable` 增加抽象 `rename()` 方法，各引擎实现自身语义。
3. SQLite `Table.rename()`：
   - 校验新表名安全性和目标冲突。
   - 关闭当前表持有的 meta/partition 连接。
   - 移动 `db_path/old` 到 `db_path/new`。
   - 重新连接并把 table meta、part manifest、column range manifest、segment manifest 中的 `table_name` 更新为新名。
4. PostgreSQL `Table.rename()`：
   - 校验关系名。
   - 检查目标关系是否存在。
   - native partition 模式下查询 `pg_inherits` 子表，并把旧前缀子表改为新前缀。
   - 更新内存中的 `_table_name`。
5. CLI 使用现有 SQLite source 解析规则，仅支持 SQLite 运维命令。
6. 更新 README/skill 可在实现后按测试结果决定是否需要补充；本次优先保持变更聚焦。

## 验证

1. 新增 tests 覆盖：
   - SQLite database 目录重命名后原路径消失、新路径可打开。
   - SQLite table 重命名后 `table_names()`、`spec()`、`select()`、`validate()` 正常，旧表不可见。
   - 纯 table、partition table、row segment table 至少覆盖 manifest 更新。
2. 运行相关测试：
   - `pytest tests/test_rename.py`
   - `pytest tests/engines/sqlite/test_partition_segment_combinations.py tests/test_cli.py`
