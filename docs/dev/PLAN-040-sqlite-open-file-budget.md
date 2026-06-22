# PLAN-040 SQLite 打开文件预算

## 背景

G1 ext4 HDD 基准前的 Linux 全量测试在默认 `ulimit -n=1024` 下触发
`OSError: [Errno 24] Too many open files`。提高系统限制后测试可以通过，但这不能
作为库的解决方案。

当前 SQLite 表虽然通过 `_num_opened_max_allowed=5` 对单表分区数据库做 LRU
淘汰，但存在以下问题：

1. 上限是私有字段，调用方无法通过稳定 API 配置。
2. 每个元数据或分区数据库仍使用低层连接池默认值 `max_connections=10`。
3. 低层 `_active_databases` 使用强引用集合，业务代码遗弃但未显式 `close()` 的
   数据库不会被垃圾回收，文件描述符持续累积。
4. 现有测试只断言分区缓存条目数，没有验证连接池上限和进程 FD 回收。

## 目标

1. 主 API 提供进程内可配置的 SQLite 资源上限，不依赖提高 `ulimit`。
2. 默认单表最多缓存 5 个打开的分区数据库。
3. 默认每个 SQLite 文件最多建立 1 条底层连接。
4. 遗弃的低层数据库对象不再被全局清理注册表永久保活。
5. 在 Linux 默认 `ulimit -n=1024` 下完成多分区写入、扫描和重复实例压力测试。

## API 设计

```python
Database(
    "sqlite://data/market.iosql",
    max_open_partitions=5,
    max_connections_per_file=1,
)
```

- `max_open_partitions`：每张逻辑表缓存的打开分区数据库数量，必须为正整数。
- `max_connections_per_file`：每个 SQLite 文件连接池允许的连接数，必须为正整数。
- 两个参数仅支持 SQLite；PostgreSQL 传入非默认资源参数时应明确拒绝。
- `Database.close()` 仍是确定性释放资源的首选方式；资源上限和弱引用回收是安全网，
  不是省略生命周期管理的理由。

## 实施步骤

1. 扩展高层 `Database` 构造参数，并传递到 SQLite `Table`。
2. SQLite `Table` 校验并保存资源上限，将连接上限用于元数据、分区和临时数据库。
3. 保留现有分区 LRU 语义，增加公开只读诊断属性。
4. 将 mysqlite 活跃实例注册表改为 `weakref.WeakSet`，避免注册表制造强引用泄漏。
5. 增加以下测试：
   - 无效资源上限被拒绝；
   - 大量分区写入和扫描期间缓存不超过配置值；
   - 每个文件的连接池不超过配置值；
   - 遗弃数据库在 GC 后释放 FD 和活跃注册项；
   - 默认 Linux FD 限制下重复创建、多分区操作不触发 `EMFILE`。
6. 更新 README、skill API 与故障排查文档。
7. 本地运行相关测试和全量测试，审查后生成 OUTCOME 并提交。
8. 将提交部署到 G1，在默认 `ulimit -n=1024` 下执行集中压力测试并记录结果。

## 验收标准

- 本地全量测试通过。
- Linux 全量测试无需修改 `ulimit` 即可通过。
- G1 压测中，单表打开分区数据库数始终不超过 `max_open_partitions`。
- 每个 SQLite 文件创建连接数始终不超过 `max_connections_per_file`。
- 反复遗弃实例并执行 GC 后，FD 不随实例数线性增长。
- README 与 `skills/iosql-usage` 明确记录配置方法和推荐值。
