# PLAN-041 SQLite 并发与进程级连接预算

## 背景

PLAN-040 已限制单表分区缓存和单文件连接数，并修复遗弃对象被全局强引用保活的
问题。复审发现仍存在四个生产风险：

1. 上限按表计算，多张活跃表或多个活跃 `Database` 仍会线性增加 FD。
2. 分区 LRU 没有锁或租约，同一 `Table` 并发查询会出现 `KeyError`，并发写入可能
   关闭其他线程正在使用的连接池。
3. 连接 checkout 与 `force_close_all_connections()` 竞态时，会在持有非重入锁时
   再次获取该锁，造成永久死锁。
4. `repartition_to()` 不继承 PRAGMA 和连接预算，异常时也不能保证关闭目标数据库。

## 目标

1. 对进程内所有 mysqlite 连接实施共享硬预算，默认最多 32 条真实 SQLite 连接。
2. 预算耗尽时优先回收其他连接池的空闲连接，而不是等待到死锁或触发 `EMFILE`。
3. 同一高层 `Table` 的并发读操作可安全跨分区执行，LRU 不关闭有租约的分区。
4. 并发写操作不再因 LRU 竞态出现 `KeyError`、关闭池或永久死锁。
5. 修复连接池关闭竞态。
6. `repartition_to()` 继承源表的 SQLite 配置，并在成功或失败时关闭目标数据库。

## API

```python
Database(
    "sqlite://data/market.iosql",
    max_open_partitions=5,
    max_connections_per_file=1,
    max_process_connections=32,
)
```

- `max_process_connections` 是进程级共享预算。
- 同一进程存在不同配置时，以当前活跃连接池请求值的最小值作为有效上限。
- 预算统计真实 `sqlite3.Connection`，不是 SQLite 文件数；WAL 仍可能为一条连接
  使用数据库、WAL 和 SHM 多个 FD。

## 实施步骤

1. 在 mysqlite 连接池层新增共享预算管理器：
   - 注册/注销连接池；
   - 创建连接前获取预算；
   - 关闭连接后归还预算；
   - 预算耗尽时跨池回收空闲连接；
   - 提供状态诊断。
2. 修正连接池关闭竞态，禁止在持有池锁时调用再次加锁的方法。
3. 高层 `Database`、SQLite `Table`、mysqlite `Database` 传递
   `max_process_connections`。
4. SQLite `Table` 增加分区缓存锁、条件变量和租约计数；所有内部分区操作改用租约
   上下文。
5. `repartition_to()` 继承 PRAGMA 和三个资源配置，并以 `try/finally` 关闭目标库。
6. 增加稳定复现复审问题的自动测试：
   - 多活跃表在低 RLIMIT 下不触发 `EMFILE`；
   - 连接预算不超过配置；
   - close/checkout 竞态不死锁；
   - 同一 Table 多线程扫描无 `KeyError`；
   - 并发写入无连接池关闭错误；
   - repartition 配置继承和异常关闭。
7. 运行针对性、低 FD、全量和 G1 测试，生成 OUTCOME 并更新 README/skill。
8. 补充两个互相独立的已有库示例：
   - 先准备并关闭独立源库，再重新打开持久化契约继续写入；
   - 先准备并关闭独立源库，再重新打开并重分区到新目标库；
   - 支持环境变量指向真实已有库，且不清理外部源库。

## 验收标准

- 进程连接计数始终不超过有效预算。
- 在 `RLIMIT_NOFILE=128` 下持续持有至少 50 张活跃表仍不触发 `EMFILE`。
- 并发读写测试在限定时间内退出且无 LRU/连接池异常。
- close/checkout 竞态测试无死锁。
- `repartition_to()` 的目标表使用源 PRAGMA 和资源配置，异常路径无残留连接。
- 两个已有库示例无需依赖其他示例即可单独运行，根目录和 skill 镜像一致。
- 本地与 Linux 全量测试通过，G1 压力测试通过。
