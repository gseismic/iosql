# save_as：保留源库的跨引擎另存接口

- 日期：2026-10-18。
- 对应计划：`docs/dev/PLAN-089-save-as.md`。

## 问题与目标

用户希望把同一批数据另存到不同 engine，保留原库以比较效率。当前 `migrate_to()`
已经复制源契约和数据、保留源业务数据；清理对象是迁移工作区和目标退休版本。
不过“迁移”的命名容易让人以为操作完成后源库会删除。

增加 `save_as()` 表达另存意图，复用现有安全迁移流程；现有 `migrate_to()` 的参数、
默认值及职责保持不变。

## 使用场景与接口

| 场景 | 频次判断 | 用法 |
| --- | --- | --- |
| 保留原库，把全部数据另存到其他 engine | 高 | `db.save_as(target_uri)` |
| 已拿到表对象，只另存这一张表 | 高 | `table.save_as(target_uri)` |
| 为性能比较确认复制行数相同 | 高 | `save_as(target_uri, verify="row-count")` |
| 只比较部分表，或变更目标表名 | 中 | `tables=["bars"]` / `tables={"bars": "archive"}` |
| 保留源布局，单独比较另一种目标布局 | 中 | `specs={"bars": {"partition": ...}}` |
| 大库预览、控制批次或锁等待 | 中低 | `dry_run` / `batch_size` / `lock_timeout` |

上述频次来自用户意图和现有示例，不是使用遥测。

Database 和 Table 提供相同的规范参数：

```python
def save_as(self, target_uri, tables=None, mode="append", batch_size=10_000,
            verify=None, dry_run=False, *, specs=None, temp_dir=None,
            lock_timeout=30.0): ...
```

- Database 的 `tables=None` 表示全库，返回 `MigrationResult`。
- Table 的 `tables=None` 表示当前表，返回 `TableMigrationResult`，不可选择其他源表。
- URI 决定目标 engine，支持 SQLite、Parquet、PostgreSQL，同 engine 的不同目标库也支持。
- `tables`、`mode`、`verify`、`dry_run`、`specs`、`temp_dir`、`lock_timeout`
  的语义与 `migrate_to()` 相同。新接口采用规范的 `tables`，不引入旧接口的
  `target_table` 兼容参数。

```python
with Database("sqlite://db/market.iosql") as source:
    result = source.save_as("parquet://db/comparison.iosql", verify="row-count")
    assert result.published and result.verified
    # source 及其表对象仍指向原 SQLite 库，可以继续查询。
```

## 行为与边界

1. 复制成功后保留源业务数据、契约和对象 URI，不切换源对象的 engine；源库可能生成
   协调文件及迁移工作目录。目标与源是独立数据副本，后续写入互不传播。
2. 目标已有数据时沿用 `mode` 的主键冲突策略，不自动清空或覆盖已有库。
   为比较完整数据集，推荐新目标和 `verify="row-count"`；该校验要求选中目标表不存在或为空。
3. 同步执行，暂存、校验后统一发布；锁、读取流限制、错误和恢复状态均复用迁移契约。
   dry-run 不连接目标。源与目标相同、互相包含时拒绝。
4. 不增加删除源库选项，不复制 engine 特有的二级索引、触发器或权限。
5. 性能示例在计时前完成复制、行数验证及查询结果校验；使用相同查询、投影和结果消费方式，
   分别报告首次读取与预热后重复读取的中位耗时。首次读取不宣称是操作系统冷缓存。
   示例只说明比较方法，规模化基准继续使用已有 benchmarks。

## 实现与验证

Database 增加显式参数的转发方法，BaseTable 增加共享转发方法，三个 engine 继承。
库级公共方法包装器必须像 `migrate_to` 一样排除 `save_as`，由迁移执行器统一申请锁，
避免外层共享租约阻止独占锁升级。

真实目录测试覆盖 SQLite/Parquet 双向及同 engine 复制、库/表范围、元数据与数据保留、
后续写入独立、选表改名与目标布局、dry-run 和失败时源目标完整性；PostgreSQL 使用现有
独立测试库 fixture 验证，有可用实例时执行。同步 README、仓库 skill 和示例副本。
