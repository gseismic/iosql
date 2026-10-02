# PLAN-080 mysqlite 复审问题修复结果

- 日期：2026-10-02
- 计划：[PLAN-080-mysqlite-review-fixes.md](PLAN-080-mysqlite-review-fixes.md)
- 状态：完成

## 修复结果

| 复审项 | 结果 |
| --- | --- |
| `execute()` / `execute_params()` 的 `commit=False` 可能随连接归还而回滚 | 池外上下文在执行前抛 `ValueError`；显式 `transaction()` 内仍由外层提交，`insert_all()` 的内部批处理事务不经过该拒绝入口。 |
| 异构行按全局 key 集合分组会改变输入顺序 | `group_rows_by_keys()` 只合并连续且 key 集合相同的行，批量 SQL 仍按输入顺序执行。 |
| 非正 `batch_size` 未被拒绝 | `insert_all()` 只接受正整数，布尔值和非整数也明确抛 `BadArgument`。 |
| 内存 URI 可绕过连接池限制 | `uri=True` 且 URI 查询参数 `mode=memory` 时拒绝创建连接池；普通文件 URI 不受该检查影响。 |
| fork 后多个池重复清空进程预算 | 进程级 fork 回调一次性重建预算和所有已注册池；连接关闭只释放本进程实际登记的连接。 |
| 多线程 fork 后继承锁可能死锁 | 子进程不获取父进程的 Condition、池锁或队列锁；重建 Condition、池锁、队列和线程本地租约。 |
| PRAGMA 修改未应用到当时借出的连接 | 池保存自己的当前配置，在连接归还后补应用；空闲连接仍即时应用。 |
| 构造时 `0` / `False` PRAGMA 被跳过 | PRAGMA 值先规范为 SQL 值，分别应用为 `0` / `1`。 |

## SQLite 引擎兼容性与既有数据

- SQLite 引擎继续使用文件数据库；其显式 `commit=True` 修改路径、正数 `insert_all(batch_size=...)` 及批处理内部事务保持兼容。
- 本次不修改数据库文件格式、不做数据迁移，也不重写已提交行。既有 iosql 数据的读取路径不变；输入顺序修复只影响修复后新发生的异构批量写入。
- SQLite 引擎的表级 `pragma()` 配置对象与底层 mysqlite 池是分开的；本次底层 PRAGMA 归还同步针对直接使用 `iosql.utils.mysqlite.Database` 的场景，不新增引擎层 PRAGMA 传播行为。

## 复审与验证范围

- 已按调用路径静态复审提交策略、批量 SQL 分组、池创建/归还/关闭、fork 子进程重置，以及 SQLite 引擎对文件池、显式提交和批量接口的使用。
- 本轮未新增、未运行测试；验证结论仅来自源码与文档静态审阅。
- 工作区中原有未跟踪文件 `.zcodeignore` 保持未修改，也未纳入本次计划文件。
