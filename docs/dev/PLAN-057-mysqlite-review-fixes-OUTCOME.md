# PLAN-057-mysqlite-review-fixes-OUTCOME

## 实施结果

全部按计划完成，另有两处计划外补充（见"计划外变更"）。

### 缺陷修复（5/5 确认属实并修复）

| # | 缺陷 | 修复前复现 | 修复后验证 |
|---|------|-----------|-----------|
| 1 | `Table._handle_error_on_connection` 在 `db.transaction()` 内无条件 rollback，schema fallback 静默清空事务内先前未提交数据 | 3 行变 2 行 | 3 行 ✓ |
| 1b | `Database.execute` 同样问题 | 3 行变 2 行 | 3 行 ✓ |
| 2 | 错误消息含子串但正则失配（如带引号列名 `"weird col"`）静默返回 None，失败写入被当作成功 | 返回 None | 重抛原始异常 ✓ |
| 3 | `insert_all(commit=False)` 参数被忽略，数据照常提交 | 数据可见且已提交 | 抛 ValueError ✓ |
| 5 | `select(limit=0)` 返回全表（SQL 语义应为 0 行） | 返回 5 行 | 返回 0 行 ✓ |

修复方式：
- 问题 1/1b：rollback 前检查 `database.in_transaction()`，显式事务上下文跳过回滚，交由外层 `transaction()` 统一处理；fallback 路径因此保住先前数据
- 问题 2：四个解析分支各补 `raise e` 兜底，保持原始异常类型不变（不改包成 UnKnownOperationalError，避免破坏上游 `except sqlite3.IntegrityError` 类匹配）
- 问题 3：真实延迟提交与连接池归还语义冲突，选择显式拒绝而非实现谎言
- 问题 5：`if limit:` → `if limit is not None:`

### 死代码清理

- 删除 `backup/` 整包（`_helpers.py` + `conv_to_iosql.py.readme`）
- table.py：`_handle_error`、`_try_commit`、`_update`、`_list_insertmany`（含 `assert 0`）、失效 `getsubsql_insert` 导入
- database.py：重复 `import sqlite3`、L291-303 注释块、`__table_names`、`_ensure_pragma`、`_ensure_all_pragma`、`pragma(ensure=)` 无效形参
- sql.py：`getsql_query_all_index`、`getsql_drop_index`、`getsql_drop_table`、`getsql_dict_update`
- exception.py：`UnKnownOperationalError`
- 日志 typo：`'已数据库 ...'` → `'已关闭数据库 ...'`

### 计划外变更

1. **补删 `_insert_one_batch`**：历史 review 文档（code-review-20260823-v030-full.md）点名过该方法，本次 review 漏查其调用方；rg 确认零调用后删除（insert_all 只走 `_insert_one_batch_on_connection`）
2. **修复实施中引入的 `Table.commit` 方法重复**：diff 自查发现删除 `_try_commit` 时误复制了一份 commit 存根，已去重——diff review 环节的价值实证
3. **`getsql_drop_index`** 与计划中三个函数同批删除（同为零引用）

### 类型化异常

- `getsql_create_table`：裸 `Exception('DuplicatedColumn')` → `DuplicateColumn(f'columns 定义存在重复列: [...]')`
- 裸 `Exception('PrimaryKeyNotInColumn: ...')` → 新增 `PrimaryKeyNotInColumn(ValueError)`（程序员参数错误，不用 OperationalError 基类避免污染 sqlite 异常兼容层）

## 影响面检查（用户要求专项核查）

**结论：对 iosql 其他部分无行为影响。**

| 核查项 | 方法 | 结论 |
|---|---|---|
| 删除符号残留引用 | rg 全文件类型扫描（py/md/ipynb/txt/sh/toml/cfg/json/yml/yaml），15+ 个符号名 + backup 路径 + conv_to_iosql | 仅 PLAN-057 自身与历史 review 文档提及（文档记录性质）；零活代码引用 |
| insert_all(commit=) 调用方 | rg 全仓库 insert_all( 调用点逐个核对 | 无任何调用方传 commit 参数；引擎层 `part_db[...].insert_all(...)` 均用默认值 |
| 回滚守卫对引擎的影响 | 引擎层 `.transaction(` 检索 = **零处调用**；`in_transaction()` 只反映显式事务上下文 | 引擎的 `auto_commit=False` 连接不走 transaction 上下文 → 守卫在引擎路径恒为 False → 行为与改动前完全一致 |
| 错误解析兜底 `raise e` | 引擎以 NoSuchTable/NoSuchColumn/DuplicateColumn 做 schema 演进控制流 | 三类消息正则均可正常解析（全量测试通过佐证）；兜底仅覆盖此前静默返回 None 的病态消息 |
| cli.py | 直接读代码（L414 `_indexes_for_part`） | 只用 `get_table_indices`/`close`，不在改动面 |
| iosql/ui/ | rg import 扫描 | 不直接依赖 mysqlite |
| limit=0 / 裸 Exception 依赖 | rg + 逐条核对 | 主 API select 为独立实现；唯一 `pytest.raises(Exception)` 包的是主 API truncate()，与 mysqlite 建表无关 |
| 全量回归 | `pytest tests -q`（改动完成后两次） | **392 passed, 16 skipped**，无新增失败 |

## 测试

- 新增 `TestPlan057Fixes` 6 个回归用例：事务内 ensure_column 保数据、execute 事务内报错由外层统一回滚、不可解析错误消息重抛、limit=0 空结果、commit=False 拒绝、建表 spec 类型化异常
- `tests/utils/mysqlite`: 49 → **55 passed**
- 复现脚本 `/tmp/opencode/confirm_mysqlite_bugs.py` 五项全部转"正常"

## 文件变更

```
iosql/utils/mysqlite/table.py      | 重写 _handle_error_on_connection、limit 判断、insert_all 校验、删 6 个死方法
iosql/utils/mysqlite/database.py   | execute 回滚守卫、pragma 签名清理、死代码、typo
iosql/utils/mysqlite/sql.py        | 类型化异常、删 4 个死函数
iosql/utils/mysqlite/exception.py  | -UnKnownOperationalError +PrimaryKeyNotInColumn
iosql/utils/mysqlite/backup/       | 整包删除
iosql/utils/mysqlite/README.md     | commit 参数说明更正
tests/utils/mysqlite/test_mysqlite.py | +6 回归用例
```

## 遗留事项（本次不处理）

- `Table.drop_all_index` 不过滤 autoindex，含主键表上会撞 SQLite 约束报错（已在示例 05 中如实演示并给出绕行姿势）
- `select` 的 order_by/option/limit 插值属模块定位内的原生 SQL 面，未加白名单校验
- 连接池每连接 SELECT 1 存活探测的开销（checkout/return 各一次）
