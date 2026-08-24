# PLAN-057-mysqlite-review-fixes

## 背景

对 `iosql.utils.mysqlite` 模块做 code review（见对话记录），确认 5 个缺陷与一批死代码。
所有缺陷已用 `/tmp/opencode/confirm_mysqlite_bugs.py` 复现验证：

| # | 缺陷 | 复现结果 |
|---|------|---------|
| 1 | `Table._handle_error_on_connection` 无条件 rollback；在 `db.transaction()` 内 yield 的是共享事务连接，schema fallback（ensure_column）触发时静默清空事务内先前未提交数据 | 预期 3 行实际 2 行 |
| 1b | `Database.execute` 同样无条件 rollback，事务内报错清空共享连接 | 预期 3 行实际 2 行 |
| 2 | 错误消息含子串但正则不匹配（如带引号列名 `"weird col"`）时既不抛翻译异常也不抛原始异常，静默返回 None，失败写入被当作成功 | 返回 None |
| 3 | `insert_all(commit=False)` 参数被完全忽略，数据照样提交 | 数据可见=1 行 |
| 5 | `select(limit=0)` 因 `if limit:` 判断返回全表，SQL 语义应为 0 行 | 返回 5 行 |

## 决策

- **问题 1/1b**：回滚前检查 `database.in_transaction()`。事务上下文中跳过 rollback——fallback 路径（加列重试）因此能保住先前数据；非 fallback 异常向上传播由外层 `transaction()` 统一回滚，语义不变。
- **问题 2**：子串命中但正则失配时重新抛出原始异常（`raise e`）。不改包成 `UnKnownOperationalError`：它会改变上游 `except sqlite3.IntegrityError` 等类型匹配。`UnKnownOperationalError` 维持零引用，随死代码删除。
- **问题 3**：真实实现"延迟提交"需要把连接留在池外，与连接池设计冲突且当前行为就是"立即提交"。选择显式拒绝：`commit=False` 抛 `ValueError` 说明原因，把谎言变成报错。README 参数表同步更新。
- **问题 4**：`getsql_create_table` 的两处裸 `Exception` 类型化：重复列 → 现有 `DuplicateColumn`；主键不在列中 → 新增 `PrimaryKeyNotInColumn(ValueError)`（程序员参数错误，非 sqlite 运行时错误，不用 OperationalError 基类避免与 sqlite 兼容层混淆）。
- **问题 5**：`if limit:` → `if limit is not None:`。
- **死代码**：删除 backup/ 整包、sql.py 三个零调用函数、table.py 四个被 `*_on_connection` 取代的私有方法、database.py 重复 import / 注释块 / `__table_names` / `_ensure_pragma*` / `pragma(ensure=)` 形参、日志 typo。
- **保留不动**：`Table.commit()/close()/cursor/conn` 存根（携带迁移指引信息）、`__init__` 导出 `Table/__version__`（独立工具层公共面，收窄有破坏风险收益低）、`getsubsql_insert`（sql.py 内部仍有两个调用方）。

## 变更清单

### iosql/utils/mysqlite/table.py
1. `_handle_error_on_connection`：开头改为仅在非事务上下文 rollback；四处解析分支补 `raise e` 兜底
2. `select(limit=...)`：`is not None`
3. `insert_all`：入口校验 `commit is not True` 时抛 ValueError
4. 删除 `_handle_error`、`_try_commit`、`_update`、`_list_insertmany`
5. 移除不再使用的 `getsubsql_insert` 导入

### iosql/utils/mysqlite/database.py
6. `execute()`：rollback 前检查 `in_transaction()`
7. 删除 L11 重复 `import sqlite3`、L291-303 注释块、`__table_names`、`_ensure_pragma`、`_ensure_all_pragma`
8. `pragma(key_or_dict, value)` 去掉无效 `ensure` 形参（`_pragma_one` 同步）
9. 日志 typo：`'已数据库 ...'` → `'已关闭数据库 ...'`

### iosql/utils/mysqlite/sql.py
10. `getsql_create_table`：裸 Exception → `DuplicateColumn` / `PrimaryKeyNotInColumn`
11. 删除 `getsql_query_all_index`、`getsql_drop_table`、`getsql_dict_update`

### iosql/utils/mysqlite/exception.py
12. 删除 `UnKnownOperationalError`，新增 `PrimaryKeyNotInColumn(ValueError)`

### 删除
13. `iosql/utils/mysqlite/backup/`（`_helpers.py` + `conv_to_iosql.py.readme`，全仓库零引用）

### iosql/utils/mysqlite/README.md
14. 批量写入参数表：`commit` 标注为必须 True，False 会报错

### tests/utils/mysqlite/test_mysqlite.py（新增回归测试）
15. 事务内 ensure_column fallback 保住先前未提交行（问题1）
16. 引号列名错误消息重抛原始异常（问题2）
17. select limit=0 返回空（问题5）
18. insert_all(commit=False) 拒绝（问题3）
19. getsql_create_table 类型化异常（问题4）

## 验证

- `pytest tests/utils/mysqlite -q` 全绿（49 + 新增）
- `pytest tests/engines/sqlite -q` 不回归（引擎以 mysqlite 异常做控制流）
- 修复后重跑 `/tmp/opencode/confirm_mysqlite_bugs.py`，全部转"正常"

## 结果

见 PLAN-057-mysqlite-review-fixes-OUTCOME.md
