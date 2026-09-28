# PLAN-065 结果：列类型、声明值域与说明元数据接口

## 完成内容

1. `TableContract` 新增 `description`、`column_descriptions`、`column_ranges`。保持原字段及构造参数位置不变；空扩展字段不进入 `to_dict()`，无穷边界在契约字典中编码为 `+inf` / `-inf`。
2. `Database.table()` 和 `Database.timeseries()` 支持新建契约时声明表介绍、字段解释和值域。已有 SQLite 表只传元信息参数时可直接增补；显式的增删操作由 `Table.update_metadata()` 完成。
3. `Table.describe()` 返回表名、表介绍、主键、按 schema 顺序排列的列名/类型/解释/值域，不扫描业务数据；旧 `column_info.comment` 作为字段解释回退。
4. SQLite 在每张表的 `meta.sqlite3` 中使用 `_iosql_metadata` sidecar 持久化新信息。读取、合并、校验和写入都在同一 `BEGIN IMMEDIATE` 事务中完成。
5. SQLite 写入 legacy meta JSON 前会从 `table_contract` / `table_spec` 剥离新字段。业务表结构、旧 `column_info`、数据统计以及原有类型字段均未改动。
6. 范围输入要求数值标量及 `low <= high`；拒绝布尔边界和 NaN。范围不限制数据写入，也不参与数据统计。
7. PostgreSQL 可从当前实例绑定的 `TableContract` 调用 `describe()`；`update_metadata()` 的持久化只在 SQLite 实现。

## 设计审查与代码复审

- 审查确认 SQLite 旧写入路径会整行重写 legacy meta，故将 sidecar 定为新元数据唯一的 SQLite 持久化来源；这一约束已落在 `_overwrite_table_meta()` 的序列化边界。
- 复审发现新表的契约在首次写入前只存在于实例内存，因此声明元信息会先做输入和 schema 校验，在首次写入生成表级 meta 时再写入 sidecar。
- 复审确认已有表增补只读写 sidecar，不调用业务表 DDL 或数据扫描。
- 复审确认旧字段 comment 只读回退，不会被新接口覆盖。
- 代码复审发现 PostgreSQL 绑定契约时没有检查 `column_descriptions` / `column_ranges` 的键是否属于已声明列，未知键会被 `describe()` 静默遗漏。现已在绑定契约、触发表创建前校验这两类键，并校验值域对应 PostgreSQL 数值类型；错误会指出未声明列或不支持的列类型。
- 检查 `git diff --check` 无空白错误；未运行测试，遵循本任务中“不主动添加或运行测试”的工作约束。

## 范围与限制

- `column_ranges` 第一版支持标量数值闭区间；数组形状、类别集合和写入值域校验仍不支持。
- PostgreSQL 的说明和值域跟随当前绑定契约，不保证重开后自动恢复；SQLite 的 `update_metadata()` 负责持久化旧表增补。
- 本次未提交代码。
